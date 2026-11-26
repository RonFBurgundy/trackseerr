"""Tests for the media-server refresh coalescing scheduler."""

import logging
from unittest.mock import MagicMock

import pytest

from trackseerr.config import Config
from trackseerr.media_servers import refresh_scheduler
from trackseerr.media_servers.base import MediaServer, MediaServerError


@pytest.fixture(autouse=True)
def reset_scheduler():
    refresh_scheduler.reset_for_tests()
    yield
    refresh_scheduler.reset_for_tests()


def test_burst_of_requests_fires_once(monkeypatch):
    t = 1000.0
    monkeypatch.setattr(refresh_scheduler, "_monotonic", lambda: t)
    target = MagicMock(spec=MediaServer)
    target.kind = "plex"

    for i in range(30):
        refresh_scheduler.request_library_refresh(target, reason=f"reason_{i}")

    assert refresh_scheduler._scheduler._timer is not None
    assert refresh_scheduler.flush_now() is True
    target.refresh_library.assert_called_once()


def test_latest_server_wins(monkeypatch):
    t = 1000.0
    monkeypatch.setattr(refresh_scheduler, "_monotonic", lambda: t)
    target1 = MagicMock(spec=MediaServer)
    target1.kind = "plex"
    target2 = MagicMock(spec=MediaServer)
    target2.kind = "jellyfin"

    refresh_scheduler.request_library_refresh(target1, reason="first")
    refresh_scheduler.request_library_refresh(target2, reason="second")

    assert refresh_scheduler.flush_now() is True
    target1.refresh_library.assert_not_called()
    target2.refresh_library.assert_called_once()


def test_second_burst_waits_for_window(monkeypatch):
    current_time = 1000.0
    monkeypatch.setattr(refresh_scheduler, "_monotonic", lambda: current_time)
    refresh_scheduler.configure(10)  # window = 600s
    target = MagicMock(spec=MediaServer)
    target.kind = "plex"

    refresh_scheduler.request_library_refresh(target, reason="first")
    assert refresh_scheduler.flush_now() is True
    target.refresh_library.assert_called_once()

    # Advance time by 30 seconds
    current_time += 30.0
    refresh_scheduler.request_library_refresh(target, reason="second")

    # Expected delay = window (600s) - 30s elapsed = 570s
    expected_delay = 600.0 - 30.0
    delay = refresh_scheduler.pending_delay()
    assert delay is not None
    assert abs(delay - expected_delay) < 1e-3


def test_zero_window_disables(caplog):
    refresh_scheduler.configure(0)
    target = MagicMock(spec=MediaServer)
    target.kind = "plex"

    with caplog.at_level(logging.DEBUG):
        refresh_scheduler.request_library_refresh(target, reason="test")
        # Second call to verify it logs at DEBUG once
        refresh_scheduler.request_library_refresh(target, reason="test2")

    assert refresh_scheduler.pending_delay() is None
    assert refresh_scheduler._scheduler._timer is None
    assert refresh_scheduler.flush_now() is False
    target.refresh_library.assert_not_called()
    assert caplog.text.count("disabled (window=0)") == 1


def test_none_server_is_noop():
    refresh_scheduler.request_library_refresh(None, reason="test")
    assert refresh_scheduler.pending_delay() is None
    assert refresh_scheduler._scheduler._timer is None
    assert refresh_scheduler.flush_now() is False


def test_refresh_error_is_logged_not_raised(caplog):
    target = MagicMock(spec=MediaServer)
    target.kind = "plex"
    target.refresh_library.side_effect = MediaServerError("Server exploded", safe_detail="Server exploded")

    refresh_scheduler.request_library_refresh(target, reason="test")
    with caplog.at_level(logging.WARNING):
        assert refresh_scheduler.flush_now() is True

    target.refresh_library.assert_called_once()
    assert "Server exploded" in caplog.text or "rescan failed" in caplog.text
    assert refresh_scheduler._scheduler._last_fired is not None


def test_info_logging_on_successful_refresh(caplog):
    target = MagicMock(spec=MediaServer)
    target.kind = "plex"

    refresh_scheduler.request_library_refresh(target, reason="import")
    refresh_scheduler.request_library_refresh(target, reason="rename")
    refresh_scheduler.request_library_refresh(target, reason="import")
    refresh_scheduler.request_library_refresh(target, reason="retag")
    refresh_scheduler.request_library_refresh(target, reason="scan")

    with caplog.at_level(logging.INFO):
        assert refresh_scheduler.flush_now() is True

    target.refresh_library.assert_called_once()
    assert "Asked plex to rescan its music library (5 change(s) batched: import, rename, retag)" in caplog.text


def test_config_parses_refresh_minutes(monkeypatch):
    monkeypatch.setenv("PLEX_URL", "http://plex:32400")
    monkeypatch.setenv("PLEX_TOKEN", "token")

    monkeypatch.setenv("MEDIA_SERVER_REFRESH_MINUTES", "5")
    assert Config.from_env().media_server_refresh_minutes == 5

    monkeypatch.setenv("MEDIA_SERVER_REFRESH_MINUTES", "abc")
    assert Config.from_env().media_server_refresh_minutes == 10

    monkeypatch.setenv("MEDIA_SERVER_REFRESH_MINUTES", "-3")
    assert Config.from_env().media_server_refresh_minutes == 0

    monkeypatch.delenv("MEDIA_SERVER_REFRESH_MINUTES", raising=False)
    assert Config.from_env().media_server_refresh_minutes == 10


def test_folder_requests_batch_into_one_paths_call(caplog):
    server = MagicMock(spec=MediaServer)
    server.kind = "plex"
    server.capabilities = MagicMock(folder_refresh=True)
    server.library_roots.return_value = ["/data/music"]
    server.refresh_paths.return_value = True

    db = MagicMock()
    db.get_media_server_path_mapping.return_value = None
    db.get_media_management_settings.return_value = {"root_folder_path": "/music"}

    refresh_scheduler.request_library_refresh(
        server, reason="first", paths=["/music/A/Album1"], db=db
    )
    refresh_scheduler.request_library_refresh(
        server, reason="second", paths=["/music/B/Album2"], db=db
    )
    refresh_scheduler.request_library_refresh(
        server, reason="third", paths=["/music/A/Album1"], db=db
    )

    with caplog.at_level(logging.INFO):
        assert refresh_scheduler.flush_now() is True

    server.refresh_paths.assert_called_once_with(
        ["/data/music/A/Album1", "/data/music/B/Album2"]
    )
    server.refresh_library.assert_not_called()
    assert "Asked plex to rescan 2 folder(s)" in caplog.text


def test_folder_only_uses_quiet_delay_not_window(monkeypatch):
    t = 1000.0
    monkeypatch.setattr(refresh_scheduler, "_monotonic", lambda: t)
    refresh_scheduler.configure(10)  # window = 600s
    server = MagicMock(spec=MediaServer)
    server.kind = "plex"
    server.capabilities = MagicMock(folder_refresh=True)
    server.library_roots.return_value = ["/music"]
    db = MagicMock()
    db.get_media_server_path_mapping.return_value = None
    db.get_media_management_settings.return_value = {"root_folder_path": "/music"}

    # Initial full rescan
    refresh_scheduler.request_library_refresh(server, reason="full")
    assert refresh_scheduler.flush_now() is True
    server.refresh_library.assert_called_once()

    # Advance time by 30 seconds
    t += 30.0
    refresh_scheduler.request_library_refresh(
        server, reason="folder", paths=["/music/A/Album1"], db=db
    )

    delay = refresh_scheduler.pending_delay()
    assert delay == 60.0


def test_full_request_absorbs_pending_folders():
    server = MagicMock(spec=MediaServer)
    server.kind = "plex"
    server.capabilities = MagicMock(folder_refresh=True)
    server.library_roots.return_value = ["/music"]
    db = MagicMock()
    db.get_media_server_path_mapping.return_value = None
    db.get_media_management_settings.return_value = {"root_folder_path": "/music"}

    refresh_scheduler.request_library_refresh(
        server, reason="folder", paths=["/music/A/Album1"], db=db
    )
    refresh_scheduler.request_library_refresh(server, reason="full")

    assert refresh_scheduler.flush_now() is True
    server.refresh_library.assert_called_once()
    server.refresh_paths.assert_not_called()


def test_untranslatable_path_falls_back_to_full():
    server = MagicMock(spec=MediaServer)
    server.kind = "plex"
    server.capabilities = MagicMock(folder_refresh=True)
    server.library_roots.return_value = ["/data/music1", "/data/music2"]
    db = MagicMock()
    db.get_media_server_path_mapping.return_value = None
    db.get_media_management_settings.return_value = {"root_folder_path": "/other"}

    refresh_scheduler.request_library_refresh(
        server, reason="folder", paths=["/unknown/A/Album1"], db=db
    )

    assert refresh_scheduler.flush_now() is True
    server.refresh_library.assert_called_once()
    server.refresh_paths.assert_not_called()


def test_saved_mapping_translates():
    server = MagicMock(spec=MediaServer)
    server.kind = "plex"
    server.paths_relative = False
    db = MagicMock()
    db.get_media_server_path_mapping.return_value = {
        "server_prefix": "/media/Music",
        "local_prefix": "/music",
        "server_kind": "",
    }

    res = refresh_scheduler.to_server_folder(db, server, "/music/A/X")
    assert res == "/media/Music/A/X"


def test_same_paths_identity():
    server = MagicMock(spec=MediaServer)
    server.kind = "plex"
    server.paths_relative = False
    server.library_roots.return_value = ["/music"]
    db = MagicMock()
    db.get_media_server_path_mapping.return_value = None

    res = refresh_scheduler.to_server_folder(db, server, "/music/A/X")
    assert res == "/music/A/X"


def test_refresh_paths_failure_falls_back_to_full(caplog):
    server = MagicMock(spec=MediaServer)
    server.kind = "plex"
    server.capabilities = MagicMock(folder_refresh=True)
    server.library_roots.return_value = ["/music"]
    server.refresh_paths.side_effect = MediaServerError("Server error", safe_detail="Server error")
    db = MagicMock()
    db.get_media_server_path_mapping.return_value = None
    db.get_media_management_settings.return_value = {"root_folder_path": "/music"}

    refresh_scheduler.request_library_refresh(
        server, reason="folder", paths=["/music/A/Album1"], db=db
    )

    with caplog.at_level(logging.WARNING):
        assert refresh_scheduler.flush_now() is True
        assert refresh_scheduler.flush_now() is True

    server.refresh_paths.assert_called_once()
    server.refresh_library.assert_called_once()
    assert "folder rescan failed" in caplog.text


def test_missing_folder_walks_up_to_existing_parent(tmp_path):
    root = tmp_path / "music"
    artist = root / "Artist"
    artist.mkdir(parents=True)
    gone_album = artist / "GoneAlbum"

    server = MagicMock(spec=MediaServer)
    server.kind = "plex"
    server.capabilities = MagicMock(folder_refresh=True)
    server.library_roots.return_value = ["/data/music"]
    server.refresh_paths.return_value = True

    db = MagicMock()
    db.get_media_server_path_mapping.return_value = None
    db.get_media_management_settings.return_value = {"root_folder_path": str(root)}

    refresh_scheduler.request_library_refresh(
        server, reason="gone", paths=[str(gone_album)], db=db
    )
    assert refresh_scheduler.flush_now() is True
    server.refresh_paths.assert_called_once_with(["/data/music/Artist"])


def test_file_path_uses_its_folder(tmp_path):
    root = tmp_path / "music"
    album = root / "Artist" / "Album"
    album.mkdir(parents=True)
    track_file = album / "01 - Track.flac"
    track_file.write_text("test")

    server = MagicMock(spec=MediaServer)
    server.kind = "plex"
    server.capabilities = MagicMock(folder_refresh=True)
    server.library_roots.return_value = ["/data/music"]
    server.refresh_paths.return_value = True

    db = MagicMock()
    db.get_media_server_path_mapping.return_value = None
    db.get_media_management_settings.return_value = {"root_folder_path": str(root)}

    refresh_scheduler.request_library_refresh(
        server, reason="track", paths=[str(track_file)], db=db
    )
    assert refresh_scheduler.flush_now() is True
    server.refresh_paths.assert_called_once_with(["/data/music/Artist/Album"])


def test_failed_folder_refresh_requeues_full_with_window(monkeypatch):
    current_time = 1000.0
    monkeypatch.setattr(refresh_scheduler, "_monotonic", lambda: current_time)
    refresh_scheduler.configure(10)  # window = 600s

    server = MagicMock(spec=MediaServer)
    server.kind = "plex"
    server.capabilities = MagicMock(folder_refresh=True)
    server.library_roots.return_value = ["/music"]
    server.refresh_paths.return_value = False

    db = MagicMock()
    db.get_media_server_path_mapping.return_value = None
    db.get_media_management_settings.return_value = {"root_folder_path": "/music"}

    # Initial full rescan
    refresh_scheduler.request_library_refresh(server, reason="initial_full")
    assert refresh_scheduler.flush_now() is True
    server.refresh_library.assert_called_once()
    server.refresh_library.reset_mock()

    # Advance time by 30 seconds
    current_time += 30.0
    refresh_scheduler.request_library_refresh(
        server, reason="folder_batch", paths=["/music/Artist/Album"], db=db
    )

    # Trigger folder refresh execution
    assert refresh_scheduler.flush_now() is True
    server.refresh_paths.assert_called_once()
    # refresh_library should NOT be called immediately
    server.refresh_library.assert_not_called()

    # Delay should be window remainder (600 - 30 = 570s)
    expected_delay = 600.0 - 30.0
    delay = refresh_scheduler.pending_delay()
    assert delay is not None
    assert abs(delay - expected_delay) < 1e-3

    # Now flush_now() calls refresh_library once
    assert refresh_scheduler.flush_now() is True
    server.refresh_library.assert_called_once()


def test_over_100_folders_does_full_rescan():
    server = MagicMock(spec=MediaServer)
    server.kind = "plex"
    server.capabilities = MagicMock(folder_refresh=True)
    server.library_roots.return_value = ["/music"]
    db = MagicMock()
    db.get_media_server_path_mapping.return_value = None
    db.get_media_management_settings.return_value = {"root_folder_path": "/music"}

    paths = [f"/music/Artist/Album{i}" for i in range(101)]
    refresh_scheduler.request_library_refresh(server, reason="bulk", paths=paths, db=db)

    assert refresh_scheduler.flush_now() is True
    server.refresh_library.assert_called_once()
    server.refresh_paths.assert_not_called()


def test_subsonic_like_server_without_folder_refresh_does_full():
    server = MagicMock(spec=MediaServer)
    server.kind = "subsonic"
    server.capabilities = MagicMock(folder_refresh=False)
    db = MagicMock()

    refresh_scheduler.request_library_refresh(
        server, reason="folder", paths=["/music/A/Album1"], db=db
    )

    assert refresh_scheduler.flush_now() is True
    server.refresh_library.assert_called_once()
    server.refresh_paths.assert_not_called()


def test_library_roots_cached(monkeypatch):
    t = 1000.0
    monkeypatch.setattr(refresh_scheduler, "_monotonic", lambda: t)
    server = MagicMock(spec=MediaServer)
    server.kind = "plex"
    server.paths_relative = False
    server.library_roots.return_value = ["/music"]
    db = MagicMock()
    db.get_media_server_path_mapping.return_value = None

    res1 = refresh_scheduler.to_server_folder(db, server, "/music/A/X")
    assert res1 == "/music/A/X"
    server.library_roots.assert_called_once()

    # Within 1 hour (1800s later)
    t += 1800.0
    res2 = refresh_scheduler.to_server_folder(db, server, "/music/B/Y")
    assert res2 == "/music/B/Y"
    server.library_roots.assert_called_once()


def test_requeued_full_overrides_pending_folder_timer(monkeypatch):
    current_time = 1000.0
    monkeypatch.setattr(refresh_scheduler, "_monotonic", lambda: current_time)
    refresh_scheduler.configure(10)  # window = 600s

    server = MagicMock(spec=MediaServer)
    server.kind = "plex"
    server.capabilities = MagicMock(folder_refresh=True)
    server.library_roots.return_value = ["/music"]
    db = MagicMock()
    db.get_media_server_path_mapping.return_value = None
    db.get_media_management_settings.return_value = {"root_folder_path": "/music"}

    # 1. Fire a full rescan to establish last_full_fired
    refresh_scheduler.request_library_refresh(server, reason="initial_full")
    assert refresh_scheduler.flush_now() is True
    server.refresh_library.assert_called_once()
    server.refresh_library.reset_mock()

    # 2. Advance time by 30 seconds
    current_time += 30.0

    # Fake refresh_paths that simulates an import arriving mid-call
    def fake_refresh_paths(paths):
        refresh_scheduler.request_library_refresh(
            server, reason="mid_call_import", paths=["/music/Artist/Album2"], db=db
        )
        return False

    server.refresh_paths.side_effect = fake_refresh_paths

    # Request first folder batch
    refresh_scheduler.request_library_refresh(
        server, reason="folder_batch", paths=["/music/Artist/Album1"], db=db
    )

    # 3. Call flush_now() for the first batch
    assert refresh_scheduler.flush_now() is True
    server.refresh_paths.assert_called_once()
    server.refresh_library.assert_not_called()

    # Delay should be window remainder (600 - 30 = 570s), NOT 60s
    expected_delay = 600.0 - 30.0
    delay = refresh_scheduler.pending_delay()
    assert delay is not None
    assert abs(delay - expected_delay) < 1e-3


def _make_fake_server(kind: str = "plex", *, folder_refresh: bool = False, roots: list[str] | None = None):
    srv = MagicMock(spec=MediaServer)
    srv.kind = kind
    srv.paths_relative = False
    srv.capabilities = MagicMock(folder_refresh=folder_refresh)
    srv.library_roots.return_value = roots if roots is not None else []
    srv.refresh_paths.return_value = True
    return srv


def _make_record(
    server_id: str,
    kind: str = "plex",
    *,
    enabled: bool = True,
    roles: frozenset[str] | None = None,
    name: str | None = None,
):
    from trackseerr.media_servers import registry

    return registry.ServerRecord(
        id=server_id,
        kind=kind,
        name=name or server_id,
        source="settings",
        url=f"http://{server_id}.local",
        api_key="tok",
        enabled=enabled,
        roles=roles if roles is not None else frozenset(registry.ROLES),
    )


def test_fans_out_to_every_refresh_server(monkeypatch):
    from trackseerr.media_servers import registry

    rec1 = _make_record("plex-1", "plex")
    rec2 = _make_record("jf-1", "jellyfin")
    fakes = {"plex-1": _make_fake_server("plex"), "jf-1": _make_fake_server("jellyfin")}
    registry.install([rec1, rec2])
    monkeypatch.setattr(registry, "build_adapter", lambda r: fakes[r.id])

    refresh_scheduler.request_library_refresh(None, reason="import")
    assert refresh_scheduler.flush_now() is True

    fakes["plex-1"].refresh_library.assert_called_once()
    fakes["jf-1"].refresh_library.assert_called_once()


def test_skips_servers_without_refresh_role_or_disabled(monkeypatch):
    from trackseerr.media_servers import registry

    active = _make_record("active", "plex")
    no_role = _make_record("no-role", "jellyfin", roles=frozenset({registry.ROLE_PLAYLISTS}))
    disabled = _make_record("disabled", "subsonic", enabled=False)
    fakes = {
        "active": _make_fake_server("plex"),
        "no-role": _make_fake_server("jellyfin"),
        "disabled": _make_fake_server("subsonic"),
    }
    registry.install([active, no_role, disabled])
    monkeypatch.setattr(registry, "build_adapter", lambda r: fakes[r.id])

    refresh_scheduler.request_library_refresh(None, reason="import")
    assert refresh_scheduler.flush_now() is True

    fakes["active"].refresh_library.assert_called_once()
    fakes["no-role"].refresh_library.assert_not_called()
    fakes["disabled"].refresh_library.assert_not_called()


def test_installed_registry_without_refresh_servers_does_nothing(monkeypatch):
    from trackseerr.media_servers import registry

    passed_server = _make_fake_server("plex")
    no_role = _make_record("no-role", "plex", roles=frozenset({registry.ROLE_PLAYLISTS}))
    fakes = {"no-role": _make_fake_server("plex")}
    registry.install([no_role])
    monkeypatch.setattr(registry, "build_adapter", lambda r: fakes[r.id])

    refresh_scheduler.request_library_refresh(passed_server, reason="import")
    assert refresh_scheduler.flush_now() is False
    passed_server.refresh_library.assert_not_called()
    fakes["no-role"].refresh_library.assert_not_called()


def test_each_server_coalesces_separately(monkeypatch):
    from trackseerr.media_servers import registry

    rec1 = _make_record("plex-1", "plex")
    rec2 = _make_record("plex-2", "plex")
    fakes = {"plex-1": _make_fake_server("plex"), "plex-2": _make_fake_server("plex")}
    registry.install([rec1, rec2])
    monkeypatch.setattr(registry, "build_adapter", lambda r: fakes[r.id])

    refresh_scheduler.request_library_refresh(None, reason="import-1")
    refresh_scheduler.request_library_refresh(None, reason="import-2")
    assert refresh_scheduler.flush_now() is True

    fakes["plex-1"].refresh_library.assert_called_once()
    fakes["plex-2"].refresh_library.assert_called_once()


def test_one_server_failing_does_not_stop_others(monkeypatch, caplog):
    from trackseerr.media_servers import registry

    rec1 = _make_record("failing", "plex", name="FailingPlex")
    rec2 = _make_record("healthy", "jellyfin", name="HealthyJF")
    failing = _make_fake_server("plex")
    failing.refresh_library.side_effect = MediaServerError("boom", safe_detail="boom")
    healthy = _make_fake_server("jellyfin")
    fakes = {"failing": failing, "healthy": healthy}
    registry.install([rec1, rec2])
    monkeypatch.setattr(registry, "build_adapter", lambda r: fakes[r.id])

    refresh_scheduler.request_library_refresh(None, reason="import")
    with caplog.at_level(logging.WARNING):
        assert refresh_scheduler.flush_now() is True

    failing.refresh_library.assert_called_once()
    healthy.refresh_library.assert_called_once()
    assert "boom" in caplog.text


def test_per_server_path_mapping_used(monkeypatch):
    from trackseerr.media_servers import registry
    from trackseerr.storage import Database

    db = Database(":memory:")
    row1 = db.insert_media_server_row(
        {"kind": "plex", "name": "PlexOne", "url": "http://plex1.local", "api_key": "t1"}
    )
    row2 = db.insert_media_server_row(
        {"kind": "plex", "name": "PlexTwo", "url": "http://plex2.local", "api_key": "t2"}
    )
    db.set_media_server_row_path_mapping(
        row1["id"], {"local_prefix": "/music", "server_prefix": "/plex1-media"}
    )
    db.set_media_server_row_path_mapping(
        row2["id"], {"local_prefix": "/music", "server_prefix": "/plex2-media"}
    )

    recs = registry.load_records(Config(plex_url="", plex_token=""), db)
    fakes = {
        row1["id"]: _make_fake_server("plex", folder_refresh=True, roots=["/plex1-media"]),
        row2["id"]: _make_fake_server("plex", folder_refresh=True, roots=["/plex2-media"]),
    }
    registry.install(recs)
    monkeypatch.setattr(registry, "build_adapter", lambda r: fakes[r.id])

    refresh_scheduler.request_library_refresh(
        None, reason="import", paths=["/music/Artist/Album"], db=db
    )
    assert refresh_scheduler.flush_now() is True

    fakes[row1["id"]].refresh_paths.assert_called_once_with(["/plex1-media/Artist/Album"])
    fakes[row2["id"]].refresh_paths.assert_called_once_with(["/plex2-media/Artist/Album"])


def test_roots_cache_is_per_server(monkeypatch):
    from trackseerr.media_servers import registry

    rec1 = _make_record("plex-1", "plex")
    rec2 = _make_record("plex-2", "plex")
    srv1 = _make_fake_server("plex", folder_refresh=True, roots=["/srv1/music"])
    srv2 = _make_fake_server("plex", folder_refresh=True, roots=["/srv2/music"])
    fakes = {"plex-1": srv1, "plex-2": srv2}
    registry.install([rec1, rec2])
    monkeypatch.setattr(registry, "build_adapter", lambda r: fakes[r.id])

    db = MagicMock()
    db.get_media_server_row.return_value = None
    db.get_media_server_path_mapping.return_value = None
    db.get_media_management_settings.return_value = {"root_folder_path": "/music"}

    refresh_scheduler.request_library_refresh(
        None, reason="import", paths=["/music/Artist/Album"], db=db
    )
    assert refresh_scheduler.flush_now() is True

    srv1.library_roots.assert_called_once()
    srv2.library_roots.assert_called_once()
    srv1.refresh_paths.assert_called_once_with(["/srv1/music/Artist/Album"])
    srv2.refresh_paths.assert_called_once_with(["/srv2/music/Artist/Album"])


def test_import_refreshes_when_no_primary_client(monkeypatch):
    from trackseerr.media_servers import registry

    rec = _make_record("nav-1", "navidrome")
    srv = _make_fake_server("navidrome", folder_refresh=False, roots=["/music"])
    registry.install([rec])
    monkeypatch.setattr(registry, "build_adapter", lambda r: srv)

    refresh_scheduler.request_library_refresh(None, reason="import")
    assert refresh_scheduler.flush_now() is True

    srv.refresh_library.assert_called_once()


