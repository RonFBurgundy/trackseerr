"""Pytest configuration and global fixtures for TrackSeerr test suite."""

import os
from pathlib import Path
import socket

# Default to legacy UI for backwards compatibility with existing frontend tests.
# New React SPA tests explicitly unset or set TRACKSEERR_LEGACY_UI to '0'.
os.environ["TRACKSEERR_LEGACY_UI"] = "1"

import pytest

# The Lidarr contract tests need a real Lidarr: not even collected unless RUN_INTEGRATION=1 (docs/INTEGRATION_TESTS.md).
collect_ignore_glob = [] if os.environ.get("RUN_INTEGRATION") == "1" else ["integration/*"]


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_metadata_network: allow real DNS/HTTP to musicbrainz.org, api.deezer.com, coverartarchive.org, "
        "itunes.apple.com (refused by default in tests)",
    )
    config.addinivalue_line(
        "markers",
        "real_mediacover_http: let MediaCoverService make real outbound HTTP (blocked by default in tests)",
    )
    config.addinivalue_line(
        "markers",
        "real_art_pipeline: keep the real art pre-cache / thumbnail pre-generate / startup-backfill schedulers "
        "instead of the autouse recording stubs",
    )
    config.addinivalue_line(
        "markers",
        "real_core_client: opt out of the autouse CoreClient.session_status mock so the real signed "
        "gateway->core call path runs (see tests/test_gateway_core_e2e.py)",
    )


@pytest.fixture(autouse=True)
def _gateway_session_status_allows_by_default(request, monkeypatch):
    """Gateway tests have no real core: let session-status say "valid" unless a test overrides it.

    Opt out with ``@pytest.mark.real_core_client`` (module-level ``pytestmark`` works too); the real
    ``CoreClient.session_status`` then runs. The session-status cache is still cleared around the test.
    """
    from plex_playlist_sync.api import dependencies
    from plex_playlist_sync.clients.core_client import CoreClient

    dependencies.clear_session_status_cache()
    if request.node.get_closest_marker("real_core_client") is None:
        monkeypatch.setattr(CoreClient, "session_status", lambda self, user_id, issued, **kw: (200, {"valid": True}))
    yield
    dependencies.clear_session_status_cache()


@pytest.fixture(autouse=True)
def _reset_media_server_process_state():
    """The Settings-page media-server overlay, its change listeners and the shared Subsonic adapter are process-wide:
    never let one test's saved settings reach the next."""
    from plex_playlist_sync import media_servers
    from plex_playlist_sync.config import set_media_server_overlay
    from plex_playlist_sync.media_servers import settings as media_server_settings

    def _reset() -> None:
        set_media_server_overlay(None)
        media_server_settings.clear_listeners()
        with media_servers._subsonic_lock:
            media_servers._subsonic_cached = None

    _reset()
    yield
    _reset()


@pytest.fixture(autouse=True)
def _restore_root_logging():
    """Tiered ``create_app`` / ``cli.main`` call ``setup_logging``, which attaches handlers bound to the
    per-test captured ``sys.stdout``. Left on the root logger they hold a closed stream and break whichever
    test runs next (order-dependent failures under xdist). Remove whatever a test added and restore the level."""
    import logging

    root = logging.getLogger()
    before_handlers = list(root.handlers)
    before_level = root.level
    yield
    for handler in list(root.handlers):
        if handler not in before_handlers and type(handler).__name__ != "LogRingBuffer":
            root.removeHandler(handler)
            handler.close()
    root.setLevel(before_level)


def _stop_worker_singletons_started_since(before: set) -> None:
    """Stop the module-level worker singletons (pending/backlog/RSS/acquisition/artist-refresh/import-list/mix)
    whose thread a test started and left running, so they cannot outlive their test's DB."""
    import threading

    from plex_playlist_sync import (
        acquisition_worker,
        artist_refresh_worker,
        backlog_worker,
        import_list_worker,
        mix_worker,
        pending_worker,
    )

    workers = {
        "PendingReleaseWorkerThread": pending_worker.pending_worker,
        "WantedBacklogWorkerThread": backlog_worker.backlog_worker,
        "RSSSyncWorkerThread": backlog_worker.rss_worker,
        "AcquisitionWorkerThread": acquisition_worker.acquisition_worker,
        "ArtistRefreshWorkerThread": artist_refresh_worker.artist_refresh_worker,
        "ImportListWorkerThread": import_list_worker.import_list_worker,
        "MixWorkerThread": mix_worker.mix_worker,
    }
    for thread in [t for t in threading.enumerate() if t not in before and t.name in workers]:
        workers[thread.name].stop()
        thread.join(timeout=10)


@pytest.fixture(autouse=True)
def _stop_scheduler_threads_started_by_the_test():
    """``cli.main`` starts the sync scheduler and the Lidarr auto-trickle runner as daemon threads that live until
    process exit. Left running they outlive their test (and its DB) for the rest of the xdist worker, burn CPU
    alongside every later test and, whenever a later test replaces ``time.sleep`` with a no-op, spin hot. Stop and
    join whatever the test started."""
    import threading

    from plex_playlist_sync import cli

    names = {"ScheduledSyncWorker", "ScheduledLidarrTrickleWorker"}
    before = set(threading.enumerate())
    yield
    _stop_worker_singletons_started_since(before)
    started = [t for t in threading.enumerate() if t.name in names and t not in before]
    if not started:
        return
    cli._shutdown_event.set()
    try:
        for thread in started:
            thread.join(timeout=10)
    finally:
        cli._shutdown_event.clear()


@pytest.fixture(autouse=True)
def _reset_library_manager_guard():
    """The work-guard counters are module state; a test that dies mid-work must not leave later tests unable to switch."""
    from plex_playlist_sync import library_manager

    with library_manager._guard_lock:
        for mode in library_manager._in_flight:
            library_manager._in_flight[mode] = 0
    yield


@pytest.fixture(autouse=True)
def _reset_lidarr_add_defaults_cache():
    """Root-folder defaults are cached per process (keyed by URL and key); tests reusing a URL must not share them."""
    from plex_playlist_sync.clients.lidarr import invalidate_add_defaults

    invalidate_add_defaults()
    yield
    invalidate_add_defaults()


@pytest.fixture(autouse=True)
def _isolated_lidarr_cover_cache(tmp_path, monkeypatch):
    """Fetched Lidarr covers are cached on disk under the mediacover base dir: keep that per-test, never shared."""
    from plex_playlist_sync.mediacover import mediacover_service

    base = tmp_path / "mediacover-base"
    monkeypatch.setattr(mediacover_service, "base_dir", base)
    # artists_dir/albums_dir are resolved once at construction (/data or /config); a stale one makes every cached
    # download fail on a read-only /data and writes outside the per-test sandbox.
    monkeypatch.setattr(mediacover_service, "artists_dir", base / "mediacover" / "artists")
    monkeypatch.setattr(mediacover_service, "albums_dir", base / "mediacover" / "albums")


class _BlockedHttp:
    """Stands in for ``mediacover.requests``: real outbound HTTP is refused unless a test mocks ``requests.get``."""

    def __init__(self) -> None:
        import requests

        self._requests = requests
        self._real_get = requests.get
        self.get = self._get
        self.blocked: list[str] = []

    def _get(self, url, *args, **kwargs):
        current = self._requests.get  # honours ``patch("requests.get")`` as well as ``patch.object(mc.requests, "get")``
        if current is not self._real_get:
            return current(url, *args, **kwargs)
        self.blocked.append(url)
        raise self._requests.ConnectionError(f"real HTTP blocked in tests: {url}")

    def __getattr__(self, name):
        return getattr(self._requests, name)


@pytest.fixture(autouse=True)
def _isolated_mediacover_service(request, monkeypatch):
    """The MediaCoverService singleton owns a background download pool, an in-flight map, a negative cache and per-host
    circuit-breaker state. Without this, one test's queued downloads (real coverartarchive/Deezer fetches) run during a
    later test and starve its waits. Real HTTP is blocked by default (opt in with ``@pytest.mark.real_mediacover_http``),
    and the pool and failure memory are reset after every test."""
    import plex_playlist_sync.mediacover as mc

    svc = mc.mediacover_service

    def _reset() -> None:
        with svc._state_lock:
            executor, svc._executor = svc._executor, None
            svc._inflight.clear()
            svc._negative.clear()
            svc._host_failures.clear()
            svc._host_open_until.clear()
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)

    _reset()  # also at setup: a stray thread from an earlier test may have dirtied it after that test's teardown
    if not request.node.get_closest_marker("real_mediacover_http"):
        monkeypatch.setattr(mc, "requests", _BlockedHttp())
    yield
    _reset()


_BANNED_HOSTS = ("musicbrainz.org", "api.deezer.com", "deezer.com", "coverartarchive.org", "itunes.apple.com")
_real_getaddrinfo = socket.getaddrinfo
_network_violations: list[str] = []


def _guarded_getaddrinfo(host, *args, **kwargs):
    name = host.decode() if isinstance(host, bytes) else str(host or "")
    if any(name == h or name.endswith("." + h) for h in _BANNED_HOSTS):
        import threading
        import traceback

        where = " <- ".join(
            f"{f.name}@{Path(f.filename).name}:{f.lineno}" for f in reversed(traceback.extract_stack()[-14:-1])
        )
        _network_violations.append(f"{name} [thread {threading.current_thread().name}] {where}")
        raise socket.gaierror(socket.EAI_NONAME, f"real network to {name} blocked in tests")
    return _real_getaddrinfo(host, *args, **kwargs)


@pytest.fixture(autouse=True)
def _no_real_metadata_network(request, monkeypatch):
    """No test may resolve musicbrainz.org, api.deezer.com, coverartarchive.org or itunes.apple.com: those must be
    mocked. Name resolution is refused for every client library (and every thread, including strays that outlive their
    test), and any attempt fails the test it landed in. Opt out with ``@pytest.mark.real_metadata_network``."""
    if request.node.get_closest_marker("real_metadata_network"):
        yield
        return
    _network_violations.clear()
    monkeypatch.setattr(socket, "getaddrinfo", _guarded_getaddrinfo)
    yield
    if _network_violations:
        seen = list(dict.fromkeys(_network_violations))
        _network_violations.clear()
        pytest.fail(f"real outbound network attempted ({len(seen)} distinct):\n" + "\n".join(seen[:5]), pytrace=False)


@pytest.fixture(autouse=True)
def _offline_cover_art_archive(monkeypatch):
    """The acquisition import path fetches an embedded-cover fallback with ``httpx.get`` straight from the Cover Art
    Archive. Answer those (and only those) with a 404 so tests that mock the MBID lookup stay offline; a test that
    patches ``httpx.get`` itself overrides this."""
    import httpx

    real_get = httpx.get

    def _get(url, *args, **kwargs):
        host = (httpx.URL(str(url)).host or "").lower()
        if any(host == h or host.endswith("." + h) for h in _BANNED_HOSTS):
            return httpx.Response(404, request=httpx.Request("GET", str(url)))
        return real_get(url, *args, **kwargs)

    monkeypatch.setattr(httpx, "get", _get)


@pytest.fixture(autouse=True)
def _reset_art_executors():
    """The art pre-cache and thumbnail pre-generation pools are module singletons that outlive a test. Cancel what is
    queued and drop the pools (a fresh one is built, with a fresh stop event, on next use) at setup and teardown."""
    from plex_playlist_sync import art_pipeline

    art_pipeline.shutdown()
    yield
    art_pipeline.shutdown()


@pytest.fixture(autouse=True)
def _fresh_album_hydration_state():
    """The hydration negative cache is process-global; a failed attempt in one test must not skip the next."""
    from plex_playlist_sync.album_track_hydration import clear_negative_cache

    clear_negative_cache()
    yield
    clear_negative_cache()


class _BackgroundJobs:
    def __init__(self) -> None:
        self.pending: list = []

    def run(self) -> int:
        """Runs the queued background jobs now, in order; returns how many ran."""
        jobs, self.pending = self.pending, []
        for _name, target in jobs:
            target()
        return len(jobs)


@pytest.fixture(autouse=True)
def background_jobs(monkeypatch):
    """Route-layer background jobs (``library._run_in_background``) are queued, not threaded: drive them with
    ``background_jobs.run()`` so tests stay deterministic and offline."""
    from plex_playlist_sync.api.routes import library

    jobs = _BackgroundJobs()
    monkeypatch.setattr(library, "_run_in_background", lambda target, name: jobs.pending.append((name, target)))
    return jobs


@pytest.fixture(autouse=True)
def _reset_artist_refresh_worker_stop_event():
    """``cli.main`` shutdown calls ``artist_refresh_worker.stop()`` on the process-wide singleton, which sets its stop
    event for good (only ``start()`` clears it, and tests stub ``start``). A later ``refresh_once`` would then abort
    its cycle immediately and refresh nothing, so clear it around every test."""
    from plex_playlist_sync.artist_refresh_worker import artist_refresh_worker

    artist_refresh_worker._stop_event.clear()
    yield
    artist_refresh_worker._stop_event.clear()


class _ArtSchedulerCalls:
    """Records calls to the art background schedulers that the autouse stub swapped in."""

    def __init__(self):
        self.precache: list[tuple] = []
        self.pregenerate: list[tuple] = []
        self.startup_backfill: list[tuple] = []


@pytest.fixture(autouse=True)
def art_scheduler_calls(request, monkeypatch):
    """Art pre-cache, thumbnail pre-generation and the startup backfill start threads/pools that outlive a test, so
    every test gets recording no-ops. Opt back in to the real ones with ``@pytest.mark.real_art_pipeline`` (or the
    ``real_art_pipeline`` fixture); the recorder is still returned but stays empty."""
    from plex_playlist_sync import art_pipeline, art_thumbs

    calls = _ArtSchedulerCalls()
    if request.node.get_closest_marker("real_art_pipeline") or "real_art_pipeline" in request.fixturenames:
        yield calls
        art_pipeline.stop_startup_backfill()
        return
    monkeypatch.setattr(
        art_pipeline, "schedule_precache", lambda *a, **kw: calls.precache.append((a, kw)) or None
    )
    monkeypatch.setattr(
        art_thumbs, "schedule_pregenerate", lambda *a, **kw: calls.pregenerate.append((a, kw)) or False
    )
    monkeypatch.setattr(
        art_pipeline, "start_startup_backfill", lambda *a, **kw: calls.startup_backfill.append((a, kw)) or None
    )
    yield calls


@pytest.fixture
def real_art_pipeline(art_scheduler_calls):
    """Opt-in marker fixture: keeps the real art schedulers for this test and drains their pools afterwards."""
    from plex_playlist_sync import art_pipeline

    yield
    art_pipeline.wait_idle(5)
