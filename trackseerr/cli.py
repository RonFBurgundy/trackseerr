import logging
import os
import signal
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Union

import uvicorn

from . import __version__
from .boot import boot_state
from .api.app import create_app
from .api.routes.sync import sync_state
from .api.routes.system.logs import get_log_file_path, log_ring_buffer
from .clients.deezer import DeezerClient
from .clients.plex import PlexClient
from .media_server import NO_MEDIA_SERVER_BOOT_MESSAGE
from .media_servers import MediaServer, MediaServerError, build_jellyfin, build_subsonic, import_server_users
from .media_servers import settings as media_server_settings
from .clients.spotify import SpotifyClient
from .clients.spotify_scraper import SpotifyWebScraper
from .config import MEDIA_SERVER_NONE, MEDIA_SERVER_PLEX, Config, ConfigError
from .log_rotation import TimedLogFileHandler, apply_saved_log_settings, numeric_level
from .redaction import RedactLogFilter, redact_sensitive_query, redact_text, safe_exc
from .security import safe_data_path
from .storage import Database
from .sync import SyncCoordinator
from .task_manager import (
    TRIGGER_SCHEDULED,
    effective_interval_seconds,
    interval_fn,
    record_task_run,
    startup_housekeeping,
    wait_for_next_cycle,
)

logger = logging.getLogger("trackseerr")
_shutdown_requested = False
# Wakes the background scheduler threads out of their waits at shutdown (and lets tests stop the ones a test
# started). They wait on this event instead of ``time.sleep`` so a patched/no-op ``time.sleep`` cannot turn them
# into hot loops.
_shutdown_event = threading.Event()


def _stopping() -> bool:
    return _shutdown_requested or _shutdown_event.is_set()


class RedactAccessLogFilter(logging.Filter):
    """Strips secrets (webhook token, API keys, OAuth state) from uvicorn access-log request paths."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(redact_sensitive_query(a) if isinstance(a, str) else a for a in record.args)
        if isinstance(record.msg, str):
            record.msg = redact_sensitive_query(record.msg)
        return True


def install_log_redaction(root_logger: logging.Logger) -> None:
    """Attaches :class:`RedactLogFilter` to every root handler (stdout, ring buffer, rotating file)."""
    for handler in root_logger.handlers:
        if not any(isinstance(f, RedactLogFilter) for f in handler.filters):
            handler.addFilter(RedactLogFilter())


def install_access_log_redaction() -> None:
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactAccessLogFilter) for f in access_logger.filters):
        access_logger.addFilter(RedactAccessLogFilter())


def _signal_handler(signum, frame):
    global _shutdown_requested
    logger.info("Received termination signal (%d). Shutting down cleanly...", signum)
    _shutdown_requested = True
    _shutdown_event.set()


_STDOUT_HANDLER_NAME = "trackseerr-stdout"


def _find_stdout_handler(root_logger: logging.Logger) -> Optional[logging.StreamHandler]:
    for h in root_logger.handlers:
        if isinstance(h, logging.StreamHandler) and getattr(h, "name", None) == _STDOUT_HANDLER_NAME:
            return h
    return None


def setup_logging(level_name: str, config: Optional[Config] = None) -> None:
    """Configures the root logger. Idempotent: repeat calls never duplicate handlers, and every root
    handler (stdout, ring buffer, rotating file, anything else attached) ends up with the redaction filter."""
    level_value = numeric_level(level_name)
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    # Line-buffer stdout so boot lines reach `docker logs` immediately, even without PYTHONUNBUFFERED.
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    except (AttributeError, ValueError, OSError):
        pass
    root_logger = logging.getLogger()
    root_logger.setLevel(level_value)
    # Importing ``api.routes.system.logs`` already attached the ring buffer to the root logger, which made
    # ``logging.basicConfig`` a silent no-op (it does nothing when the root has any handler) and left
    # the container with no stdout handler at all. Add one explicitly, identified by name so a second
    # call reuses it (re-pointed at the current ``sys.stdout``) instead of stacking another.
    stdout_handler = _find_stdout_handler(root_logger)
    if stdout_handler is None:
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.set_name(_STDOUT_HANDLER_NAME)
        stdout_handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
        root_logger.addHandler(stdout_handler)
    elif stdout_handler.stream is not sys.stdout:
        stdout_handler.setStream(sys.stdout)
    stdout_handler.setLevel(level_value)
    stdout_handler.follows_log_level = True  # type: ignore[attr-defined]

    # Attach LogRingBuffer to root logger
    if log_ring_buffer not in root_logger.handlers:
        root_logger.addHandler(log_ring_buffer)
    log_ring_buffer.setLevel(level_value)
    log_ring_buffer.follows_log_level = True

    # Attach the time-rotating file handler (trackseerr.txt; rotation/retention/size limits are tunable live).
    try:
        log_path = get_log_file_path(config)
        has_file_handler = any(
            isinstance(h, TimedLogFileHandler) and getattr(h, "baseFilename", "") == str(log_path)
            for h in root_logger.handlers
        )
        if not has_file_handler:
            file_handler = TimedLogFileHandler(log_path)
            file_handler.setLevel(level_value)
            file_handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
            root_logger.addHandler(file_handler)
    except (OSError, ValueError) as ex:
        logger.warning("Could not initialize the log file handler: %s", safe_exc(ex))

    # httpx/httpcore log every request URL at INFO, which would carry invite/reset tokens
    # relayed by the gateway. Keep them quiet, and redact whatever else reaches a handler.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    install_log_redaction(root_logger)


@dataclass
class _Clients:
    """External-service clients; filled in after the HTTP server is up so slow probes never delay binding."""

    # The connected media-server client: a PlexClient, or the Subsonic adapter (anything ``as_media_server`` accepts).
    plex: Optional[Union[PlexClient, MediaServer]] = None
    spotify: Optional[Union[SpotifyClient, SpotifyWebScraper]] = None
    deezer: Optional[DeezerClient] = None
    # Serialises replacing ``plex`` (the boot connect and Settings-triggered reconnects run on different threads).
    # Readers just read the attribute once: a reference swap is atomic and the old adapter lives on for whoever holds it.
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


def _connect_clients(config: Config, clients: _Clients, *, fatal_plex: bool) -> bool:
    """Connects Plex (when it is the configured media server), Spotify and Deezer. Returns False only when
    Plex is configured, ``fatal_plex`` is set and Plex is unreachable. With no media server nothing is connected."""
    if config.subsonic_configured:
        with boot_state.step_timer("connecting to Subsonic"):
            adapter = build_subsonic(config)  # lazy: no network here; unreachability surfaces per operation
            with clients.lock:
                clients.plex = adapter
    if config.jellyfin_configured:
        with boot_state.step_timer("connecting to Jellyfin"):
            adapter = build_jellyfin(config)  # lazy: no network here; unreachability surfaces per operation
            with clients.lock:
                clients.plex = adapter
    if config.plex_enabled:
        with boot_state.step_timer("connecting to Plex"):
            try:
                clients.plex = PlexClient(
                    base_url=config.plex_url,
                    token=config.plex_token,
                    verify_ssl=config.plex_verify_ssl,
                    music_section=config.plex_music_section,
                )
            except Exception as e:  # PlexServer raises a wide set (requests, plexapi, ssl); root cause is logged
                if fatal_plex:
                    logger.error("Failed to connect to Plex Media Server: %s", safe_exc(e))
                    logger.debug("Plex connect traceback", exc_info=True)
                    return False
                logger.warning(
                    "Could not connect to Plex Server at %s on startup: %s. "
                    "Starting Web Server; connection will be retried during sync.",
                    redact_text(config.plex_url),
                    safe_exc(e),
                )

    with boot_state.step_timer("initializing Spotify/Deezer clients"):
        if config.has_spotify:
            try:
                clients.spotify = SpotifyClient(
                    client_id=config.spotify_client_id,  # type: ignore
                    client_secret=config.spotify_client_secret,  # type: ignore
                )
            except Exception as e:  # spotipy surfaces many types; fall back to the scraper and log why
                logger.error("Failed to initialize Spotify client: %s. Falling back to web scraper.", safe_exc(e))
                clients.spotify = SpotifyWebScraper()
        else:
            logger.info("No Spotify API credentials configured; activating keyless SpotifyWebScraper")
            clients.spotify = SpotifyWebScraper()

        if config.has_deezer:
            try:
                clients.deezer = DeezerClient()
            except Exception as e:  # deezer-python raises varied types; sync skips Deezer and we log why
                logger.error("Failed to initialize Deezer client: %s. Skipping Deezer sync.", safe_exc(e))
    return True


def _make_plex_provider(
    config: Config, clients: _Clients, retry_interval: float = 60.0
) -> Callable[[], Optional[PlexClient]]:
    """Returns a callable yielding the current Plex client, re-attempting a connection when Plex was
    unreachable at boot. Attempts are spaced at least ``retry_interval`` seconds apart (no hot loop)."""
    lock = threading.Lock()
    state = {"last_attempt": time.monotonic(), "warned": False}  # the boot connect counts as attempt #1

    def provider() -> Optional[PlexClient]:
        if clients.plex is not None or not config.plex_enabled:
            return clients.plex
        with lock:
            if clients.plex is not None:
                return clients.plex
            now = time.monotonic()
            if now - state["last_attempt"] < retry_interval:
                return None
            state["last_attempt"] = now
            try:
                clients.plex = PlexClient(
                    base_url=config.plex_url,
                    token=config.plex_token,
                    verify_ssl=config.plex_verify_ssl,
                    music_section=config.plex_music_section,
                )
                logger.info("Connected to Plex Media Server after an earlier failure")
            except Exception as e:  # PlexServer raises a wide set (requests, plexapi, ssl); root cause is logged
                log = logger.debug if state["warned"] else logger.warning
                log("Plex still unreachable (will retry in %.0fs): %s", retry_interval, safe_exc(e))
                state["warned"] = True
            return clients.plex

    return provider


def _db_base_dir(config: Config) -> str:
    return config.config_dir if (os.getenv("CONFIG_DIR") or os.path.isdir("/config")) else config.data_dir


def _apply_saved_media_server_settings(config: Config) -> None:
    """Run-once and headless runs open no database, but they usually share the data directory with a web instance
    where the media server was chosen on the Settings page. When the environment names no media server and that
    database already exists, load the saved choice (read once, then closed) so these runs sync to the same server.
    A missing or unreadable database leaves the environment-only behaviour in place."""
    if config.media_server_env_controlled:
        return
    try:
        db_path = safe_data_path("sync_db.sqlite", base_dir=_db_base_dir(config))
        if not os.path.exists(db_path):
            return
        db = Database(str(db_path))
    except (PermissionError, sqlite3.Error, OSError, ValueError) as e:
        logger.warning("Could not read the saved media-server settings: %s", safe_exc(e))
        return
    try:
        media_server_settings.load_into_process(db)
        config.apply_media_server_overlay()
    finally:
        db.close()


def _discover_plex_users(db: Database, plex_client: PlexClient) -> None:
    """Auto-discover Plex Home users and populate the database."""
    try:
        home_users = plex_client.get_home_users()
        for u in home_users:
            uname = u.get("username") or u.get("name") or "Unknown"
            admin_flag = bool(u.get("is_admin", u.get("admin", False)))
            if db.is_tombstoned(str(u["id"])):
                continue  # deleted by an admin; only an explicit restore lets them back in
            if db.import_media_server_user(
                str(u["id"]), str(uname), u.get("email"), auth_type="plex", grant_admin=admin_flag
            ) is None:
                logger.warning("Skipped Plex user '%s': that name already belongs to a different Trackseerr account", uname)
        logger.info("Successfully discovered %d Plex Home users", len(home_users))
    except Exception as e:  # network + plexapi + sqlite; startup must continue, root cause is logged
        logger.warning("Could not auto-discover Plex Home users on startup: %s", safe_exc(e))


def _discover_media_server_users(db: Database, server: MediaServer) -> None:
    """Populate the user table from a non-Plex media server that has real accounts (Jellyfin), so playlists can be
    targeted at them. Never raises: startup (or the Settings reconnect thread) continues when the server is unreachable
    or one account cannot be stored; the cause is logged."""
    try:
        users = server.list_users()
    except MediaServerError as exc:
        logger.warning("Could not discover %s users on startup: %s", server.kind, exc.safe_detail)
        return
    except Exception as exc:  # an adapter bug or an unexpected payload must not stop the boot; root cause is logged
        logger.warning("Could not discover %s users on startup: %s", server.kind, safe_exc(exc))
        logger.debug("User discovery traceback", exc_info=True)
        return
    try:
        imported, skipped = import_server_users(db, server.kind, users)
    except Exception as exc:  # the import loop guards each account; this is the last resort for the loop itself
        logger.warning("Importing %s users failed: %s", server.kind, safe_exc(exc))
        logger.debug("User import traceback", exc_info=True)
        return
    logger.info("Discovered %d %s users (%d imported, %d skipped)", len(users), server.kind, imported, skipped)


def _start_sync_scheduler(
    db: Database,
    config: Config,
    clients: _Clients,
    clients_ready: Optional[threading.Event] = None,
) -> None:
    """Starts the periodic sync thread. When ``clients_ready`` is given, a cycle never runs before it is
    set (the connect step sets it on success *or* failure), so a slow Plex/Spotify probe cannot make
    the first cycle silently skip services that simply were not connected yet."""

    sync_interval = interval_fn(db, config, "playlist_sync")  # DB override > WAIT_SECONDS > default, re-read each second

    def background_sync_worker() -> None:
        logger.info("Background sync scheduler started (interval: %d seconds)", sync_interval())
        while not _stopping():
            wait_for_next_cycle(_shutdown_event, sync_interval)
            if clients_ready is not None:
                while not _stopping() and not clients_ready.wait(timeout=1.0):
                    pass
            if _stopping():
                break
            try:
                logger.info("Triggering scheduled background synchronization...")
                with record_task_run(db, "playlist_sync", TRIGGER_SCHEDULED) as run:
                    run.apply_result(
                        sync_state.execute_sync(
                            db=db,
                            config=config,
                            plex_client=clients.plex,
                            spotify_client=clients.spotify,
                            deezer_client=clients.deezer,
                        )
                    )
            except Exception as e:  # keep the scheduler alive; the root cause is logged
                logger.error("Error in scheduled background sync: %s", safe_exc(e))
                logger.debug("Scheduled sync traceback", exc_info=True)

    threading.Thread(target=background_sync_worker, daemon=True, name="ScheduledSyncWorker").start()


def _start_lidarr_trickle(db: Database, config: Config) -> None:
    from trackseerr.clients.lidarr import LidarrClient
    from trackseerr.library_manager import get_library_mode
    from trackseerr.lidarr_queue import lidarr_worker
    from trackseerr.storage import lidarr_item_due

    def background_lidarr_trickle_worker() -> None:
        logger.info("Lidarr auto-trickle background runner started")
        last_run_time = 0.0
        while not _stopping():
            _shutdown_event.wait(5)
            if _stopping():
                break
            try:
                lidarr_settings = db.get_lidarr_settings()
                if not isinstance(lidarr_settings, dict):
                    continue
                if get_library_mode(db) != "lidarr":
                    continue  # native mode: nothing may be sent to Lidarr
                auto_trickle = bool(lidarr_settings.get("auto_trickle", config.lidarr_auto_trickle))
                url = lidarr_settings.get("url") or config.lidarr_url
                api_key = lidarr_settings.get("api_key") or config.lidarr_api_key

                if not (auto_trickle and url and api_key):
                    continue

                # DB override > Lidarr settings > env/config > 30m; read every pass so an edit applies at once.
                interval_sec = max(60, effective_interval_seconds(db, config, "lidarr_auto_trickle") or 30 * 60)
                now = time.time()
                if now - last_run_time < interval_sec:
                    continue

                last_run_time = now

                if not lidarr_worker.is_running():
                    all_missing = db.get_missing_tracks()
                    # Only items whose retry time has passed: "unavailable" is re-checked weekly, errors back off.
                    unmonitored = [t for t in all_missing if lidarr_item_due(t)]
                    if unmonitored:
                        batch_size = int(
                            lidarr_settings.get("trickle_batch_size")
                            or config.lidarr_trickle_batch_size
                            or 25
                        )
                        delay_seconds = float(
                            lidarr_settings.get("trickle_rate_seconds")
                            or config.lidarr_trickle_rate_seconds
                            or 3.0
                        )
                        auto_search = bool(lidarr_settings.get("auto_search", config.lidarr_auto_search))
                        root_folder = lidarr_settings.get("root_folder") or config.lidarr_root_folder

                        lidarr_cli = LidarrClient(
                            base_url=str(url),
                            api_key=str(api_key),
                            verify_ssl=config.plex_verify_ssl,
                            auto_search=auto_search,
                            root_folder=root_folder,
                            prefer_singles=bool(lidarr_settings.get("prefer_singles", True)),
                        )
                        logger.info(
                            "Auto-trickle: enqueuing %d unmonitored tracks into Lidarr (batch: %d, pacing: %.1fs)",
                            min(len(unmonitored), batch_size),
                            batch_size,
                            delay_seconds,
                        )
                        lidarr_worker.start_trickle(
                            items=unmonitored,
                            client=lidarr_cli,
                            db=db,
                            delay_seconds=delay_seconds,
                            auto_search=auto_search,
                            batch_size=batch_size,
                            trigger=TRIGGER_SCHEDULED,
                        )
            except Exception as e:  # keep the runner alive; the root cause is logged
                logger.error("Error in scheduled Lidarr auto-trickle: %s", safe_exc(e))
                logger.debug("Auto-trickle traceback", exc_info=True)

    threading.Thread(
        target=background_lidarr_trickle_worker, daemon=True, name="ScheduledLidarrTrickleWorker"
    ).start()


def _start_local_workers(db: Database, config: Config) -> None:
    """Starts the workers that need no network at start-up (they probe their services lazily)."""
    from .backlog_worker import backlog_worker, rss_worker

    # Runs left "running" by a previous process cannot still be running; close them before any worker records new ones.
    startup_housekeeping(db)

    if config.enable_backlog_search:
        logger.info("Starting WantedBacklogWorker (interval: %d min)", config.backlog_search_interval_minutes)
        backlog_worker.start(
            db=db,
            interval_seconds=config.backlog_search_interval_minutes * 60,
            interval_fn=interval_fn(db, config, "wanted_backlog_sweep"),
        )

    if config.enable_rss_sync:
        logger.info("Starting RSSSyncWorker (interval: %d min)", config.rss_sync_interval_minutes)
        rss_worker.start(
            db=db,
            interval_seconds=config.rss_sync_interval_minutes * 60,
            interval_fn=interval_fn(db, config, "indexer_rss_sync"),
        )

    from .pending_worker import pending_worker

    logger.info("Starting PendingReleaseWorker (delay-profile releases, interval: 60s)")
    pending_worker.start(db=db, interval_seconds=60)

    from .artist_refresh_worker import artist_refresh_worker

    logger.info("Starting ArtistRefreshWorker (interval: 24h, pace: 1.5s, first cycle in 10 min)")
    artist_refresh_worker.start(
        db=db,
        interval_seconds=86400,
        pace_delay=1.5,
        interval_fn=interval_fn(db, config, "artist_metadata_refresh"),
    )

    from . import art_pipeline

    try:
        art_pipeline.start_startup_backfill(db)  # one-off after upgrade; background thread, marker-gated
    except Exception as exc:
        logger.error("Art backfill could not be started: %s", safe_exc(exc))
    # Daily (editable) missing-only pass; the last run is read from the task history so a restart does not repeat it.
    art_pipeline.art_backfill_scheduler.start(db, interval_fn(db, config, "art_thumbnail_backfill"))

    if config.enable_import_lists:
        from .import_list_worker import import_list_worker

        logger.info("Starting ImportListWorker (checks every 5 min for due lists)")
        import_list_worker.start(db=db, config=config)

    from .scrobble_worker import scrobble_worker

    logger.info("Starting ScrobbleWorker (history poll + forward retry)")
    scrobble_worker.start(db=db, config=config)

    from .mix_worker import mix_worker

    logger.info("Starting MixWorker (hourly tailored mix regeneration)")
    mix_worker.start(db=db, config=config, interval_fn=interval_fn(db, config, "mix_generation"))

    from .library_health import library_health_worker

    logger.info("Starting LibraryHealthWorker (weekly media-server reconciliation, when enabled)")
    library_health_worker.start(db=db, config=config)

    from .seed_cleanup import seed_cleanup_worker

    logger.info("Starting SeedCleanupWorker (daily finished-torrent sweep)")
    seed_cleanup_worker.start(db=db, config=config, interval_fn=interval_fn(db, config, "seed_cleanup"))

    from .recycle_bin import recycle_bin_worker

    logger.info("Starting RecycleBinWorker (daily recycle bin cleanup)")
    recycle_bin_worker.start(db=db, config=config, interval_fn=interval_fn(db, config, "recycle_bin_cleanup"))

    from .backup import backup_worker

    logger.info("Starting BackupWorker (database backup & retention pruning)")
    backup_worker.start(db=db, config=config, interval_fn=interval_fn(db, config, "backup"))

    from .update_check import update_check_worker

    logger.info("Starting UpdateCheckWorker (software update check)")
    update_check_worker.start(db=db, config=config, interval_fn=interval_fn(db, config, "update_check"))


def _log_when_listening(server: uvicorn.Server, host: str, port: int, started_at: float) -> None:
    """Logs a ``[boot]`` line, with time since process start, once uvicorn has bound its socket."""
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and not server.should_exit:
        if server.started:
            logger.info(
                "[boot] step: server listening on %s:%d (%.2fs after process start)",
                host,
                port,
                time.monotonic() - started_at,
            )
            return
        time.sleep(0.05)


def _background_init(
    *,
    role: str,
    db: Database,
    config: Config,
    clients: _Clients,
    server: uvicorn.Server,
    failure: dict[str, int],
    prehandshake: Optional[object] = None,
) -> None:
    """Post-bind startup. Anything slow or network-bound runs here, never before the server binds.

    Gateway: waits for the core protocol handshake (retry up to 60s, then fail closed), then readiness flips.
    Core / all-in-one: starts the local workers, flips readiness, then probes Plex and friends in the
    background so an unreachable Plex never keeps the UI waiting.
    """
    try:
        if role == "gateway":
            from .clients.core_client import CoreClient
            from .gateway_link import HANDSHAKE_OK, ProtocolMismatch, gateway_link_worker, perform_handshake

            link_client = CoreClient(config.trackseerr_core_url or "", config.internal_core_secret)
            try:
                if prehandshake is not None and getattr(prehandshake, "state", None) == HANDSHAKE_OK:
                    handshake = prehandshake  # core already answered the pre-bind probe
                else:
                    with boot_state.step_timer("waiting for core (protocol handshake)"):
                        handshake = perform_handshake(
                            link_client, should_stop=lambda: _shutdown_requested or server.should_exit
                        )
            except ProtocolMismatch as e:
                logger.error("%s", e)
                print(f"ERROR: {e}", file=sys.stderr)
                failure["code"] = 1
                server.should_exit = True
                return
            with boot_state.step_timer("starting gateway link worker"):
                gateway_link_worker.start(
                    link_client, db.count_active_sessions, handshaken=handshake.state == HANDSHAKE_OK  # type: ignore[attr-defined]
                )
            boot_state.mark_ready()
            logger.info("[boot] ready after %.2fs", boot_state.elapsed())
            return

        clients_ready = threading.Event()
        with boot_state.step_timer("starting background workers"):
            if config.wait_seconds > 0:
                _start_sync_scheduler(db, config, clients, clients_ready)
            from .library_manager import migrate_library_mode

            migrate_library_mode(db, config)  # before any worker can route a request
            _start_lidarr_trickle(db, config)
            _start_local_workers(db, config)
        boot_state.mark_ready()
        logger.info("[boot] ready after %.2fs (external connection checks continue in the background)", boot_state.elapsed())
    except Exception as e:  # last resort for a daemon thread: fail loudly instead of staying "starting" forever
        logger.critical("Startup failed: %s", safe_exc(e))
        logger.debug("Startup traceback", exc_info=True)
        failure["code"] = 1
        server.should_exit = True
        return

    # Past this point the instance is already serving. A failure here degrades it (logged at ERROR) but
    # must never take the process down.
    try:
        _connect_clients(config, clients, fatal_plex=False)
    except Exception as e:
        logger.error("Connecting external clients failed; continuing degraded: %s", safe_exc(e))
        logger.debug("Client connect traceback", exc_info=True)
    finally:
        clients_ready.set()  # success or failure: the scheduler must not wait forever
    if isinstance(clients.plex, MediaServer) and clients.plex.capabilities.users:
        try:
            with boot_state.step_timer(f"discovering {clients.plex.kind} users", publish=False):
                _discover_media_server_users(db, clients.plex)
        except Exception as e:  # mirrors the Plex path: user discovery degrades, it never blocks the workers below
            logger.error("%s user discovery failed; continuing degraded: %s", clients.plex.kind, safe_exc(e))
            logger.debug("User discovery traceback", exc_info=True)
    if clients.plex is not None and not isinstance(clients.plex, MediaServer):  # a raw Plex client, not a MediaServer adapter
        try:
            with boot_state.step_timer("discovering Plex Home users", publish=False):
                _discover_plex_users(db, clients.plex)
        except Exception as e:
            logger.error("Plex user discovery failed; continuing degraded: %s", safe_exc(e))
            logger.debug("Plex discovery traceback", exc_info=True)
    try:
        from .acquisition_worker import acquisition_worker

        with boot_state.step_timer("starting AcquisitionWorker", publish=False):
            acquisition_worker.start(
                db=db, plex_client_provider=_make_plex_provider(config, clients), poll_interval=5.0
            )
    except Exception as e:
        logger.error("AcquisitionWorker failed to start; continuing degraded: %s", safe_exc(e))
        logger.debug("AcquisitionWorker traceback", exc_info=True)
    logger.info("[boot] complete after %.2fs", boot_state.elapsed())


def main() -> int:  # noqa: C901, PLR0915
    process_started = time.monotonic()
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Logging first, before anything slow, so even a crash during boot leaves a trail in `docker logs`.
    setup_logging(os.getenv("LOG_LEVEL", "INFO"))
    role = os.getenv("ROLE", "all-in-one").lower().strip()
    logger.info("Initializing TrackSeerr v%s (role=%s, tier=%s)", __version__, role, role)

    with boot_state.step_timer("config load", publish=False):
        config = Config.from_env()
    setup_logging(config.log_level, config=config)
    logger.info("[boot] log level %s, config dir %s", config.log_level.upper(), config.config_dir)

    if role in ("gateway", "core"):
        from trackseerr.internal_auth import MIN_SECRET_LENGTH, validate_secret_strength

        if not validate_secret_strength(config.internal_core_secret):
            logger.error(
                "ROLE=%s requires INTERNAL_CORE_SECRET of at least %d characters (generate one with: openssl rand -hex 32).",
                role,
                MIN_SECRET_LENGTH,
            )
            return 1

    if role in ("gateway", "core"):
        from trackseerr.role_guard import README_HINT, check_role_environment

        with boot_state.step_timer("role guard", publish=False):
            problems = check_role_environment(role, os.environ)
        for problem in problems:
            if not problem.fatal:
                logger.warning("ROLE=%s: %s", role, problem.message)
        fatal = [p for p in problems if p.fatal]
        if fatal:
            for problem in fatal:
                print(f"ERROR: ROLE={role}: {problem.message}", file=sys.stderr)
            print(f"ERROR: refusing to start. {README_HINT}", file=sys.stderr)
            return 1

    if role != "gateway":
        try:
            config.validate_media_server()
        except ConfigError as e:
            logger.error("%s", e)
            print(f"ERROR: {e}", file=sys.stderr)
            return 1
        if config.media_server_type == MEDIA_SERVER_NONE:
            logger.info(NO_MEDIA_SERVER_BOOT_MESSAGE)

    clients = _Clients()

    if role != "gateway":
        from trackseerr.backup import apply_pending_restore

        db_base_dir = _db_base_dir(config)
        db_path = str(safe_data_path("sync_db.sqlite", base_dir=db_base_dir))
        try:
            apply_pending_restore(db_path)
        except Exception as exc:
            logger.critical("Error applying pending restore: %s", safe_exc(exc))

    # Run-once and headless modes have no web server, so the connection checks stay synchronous.
    if role != "gateway" and (config.run_once or config.headless):
        _apply_saved_media_server_settings(config)
        if not _connect_clients(config, clients, fatal_plex=True):
            return 1
        coordinator = SyncCoordinator(
            config=config,
            plex_client=clients.plex,
            spotify_client=clients.spotify,
            deezer_client=clients.deezer,
        )

        # 1. Run-once / CLI mode
        if config.run_once:
            logger.info("RUN_ONCE enabled; running single sync cycle and exiting.")
            coordinator.run_sync_cycle()
            logger.info("TrackSeerr run-once completed cleanly.")
            return 0

        # 2. Headless mode (no web UI)
        logger.info("Running in HEADLESS loop mode.")
        while not _shutdown_requested:
            try:
                coordinator.run_sync_cycle()
            except Exception as e:  # keep the loop alive; the root cause is logged
                logger.error("Unexpected error occurred during sync cycle: %s", safe_exc(e))
                logger.debug("Sync cycle traceback", exc_info=True)
            slept = 0
            while slept < config.wait_seconds and not _shutdown_requested:
                time.sleep(min(1, config.wait_seconds - slept))
                slept += 1
        logger.info("TrackSeerr terminated cleanly.")
        return 0

    # 3. Web UI & REST Server Mode (Default)
    db_base_dir = _db_base_dir(config)
    with boot_state.step_timer("database open + migrations", publish=False):
        if role == "gateway":
            try:
                db_path = str(safe_data_path("sync_db.sqlite", base_dir=db_base_dir))
                db = Database(db_path)
            except (PermissionError, sqlite3.OperationalError, OSError, ValueError) as e:
                fallback_db_path = "/tmp/trackseerr_gateway.sqlite"
                logger.warning(
                    "Gateway mode unable to open database at '%s': %s. "
                    "Falling back to ephemeral database at '%s'.",
                    db_base_dir,
                    e,
                    fallback_db_path,
                )
                os.environ["DATABASE_PATH"] = fallback_db_path
                db = Database(fallback_db_path)
        else:
            db_path = str(safe_data_path("sync_db.sqlite", base_dir=db_base_dir))
            try:
                db = Database(db_path)
            except (PermissionError, sqlite3.OperationalError) as e:
                logger.critical(
                    "Failed to initialize SQLite database at '%s': %s. "
                    "Please verify file and directory permissions on '%s' (e.g. Unraid PUID/PGID).",
                    db_path,
                    e,
                    db_base_dir,
                )
                return 1

    apply_saved_log_settings(db)  # saved level/rotation settings override the LOG_LEVEL env default

    # Remember the role this database last ran as; on a flip (all-in-one <-> core) log the one-time checklist.
    try:
        from .role_change import record_boot_role

        record_boot_role(db, role)
    except sqlite3.Error as e:
        logger.warning("Could not record the deployment role: %s", safe_exc(e))

    if role != "gateway":
        # The Settings page may name the media server when the environment does not; follow later saves live.
        media_server_settings.load_into_process(db)
        config.apply_media_server_overlay()

        def _reconnect_media_server() -> None:
            with clients.lock:  # one reconnect at a time: the overlay, the new adapter and the assignment go together
                config.apply_media_server_overlay()
                adapter: Optional[Union[PlexClient, MediaServer]] = clients.plex
                if clients.plex is None or isinstance(clients.plex, MediaServer):  # never replace a connected Plex client
                    if config.subsonic_configured:
                        adapter = build_subsonic(config)
                    elif config.jellyfin_configured:
                        adapter = build_jellyfin(config)
                    else:
                        adapter = None
                    clients.plex = adapter  # a sync in flight keeps the adapter it already holds
            if isinstance(adapter, MediaServer) and adapter.capabilities.users:
                threading.Thread(  # off the request thread: the server may be slow or down
                    target=_discover_media_server_users,
                    args=(db, adapter),
                    name="media-server-user-discovery",
                    daemon=True,
                ).start()
            logger.info("Media server settings changed: now using '%s'", config.media_server_type)

        media_server_settings.on_change(_reconnect_media_server)

    if role != "gateway" and config.media_server_type != MEDIA_SERVER_PLEX:  # no Plex owner can sign in as first admin
        from .admin_bootstrap import ensure_bootstrap_admin

        ensure_bootstrap_admin(db)

    # Sync legacy config playlist IDs to DB if any (local writes only)
    if role != "gateway":
        for sp_id in config.spotify_playlist_ids:
            if not db.get_playlist(sp_id):
                db.upsert_playlist(sp_id, f"Spotify Playlist {sp_id}", service="spotify")
        for dz_id in config.deezer_playlist_ids:
            if not db.get_playlist(dz_id):
                db.upsert_playlist(dz_id, f"Deezer Playlist {dz_id}", service="deezer")

    # A protocol mismatch must refuse to start before anything is served. One quick attempt (bounded by the
    # client timeout) runs here; if core is merely unreachable the full 60s retry loop continues after bind.
    prehandshake = None
    if role == "gateway":
        from .clients.core_client import CoreClient
        from .gateway_link import ProtocolMismatch, perform_handshake

        try:
            with boot_state.step_timer("core protocol check", publish=False):
                prehandshake = perform_handshake(
                    CoreClient(config.trackseerr_core_url or "", config.internal_core_secret),
                    deadline_seconds=0,
                )
        except ProtocolMismatch as e:
            logger.error("%s", e)
            print(f"ERROR: {e}", file=sys.stderr)
            db.close()
            return 1

    with boot_state.step_timer("building web application", publish=False):
        app = create_app(db=db, config=config)

    install_access_log_redaction()
    uvicorn_config = uvicorn.Config(
        app=app,
        host=config.host,
        port=config.port,
        log_level=config.log_level.lower(),
        access_log=False,
    )
    server = uvicorn.Server(uvicorn_config)
    failure: dict[str, int] = {"code": 0}
    boot_state.begin("starting")
    logger.info("[boot] step: binding web server on %s:%d (role=%s)", config.host, config.port, role)
    init_thread = threading.Thread(
        target=_background_init,
        kwargs={"role": role, "db": db, "config": config, "clients": clients, "server": server, "failure": failure,
                "prehandshake": prehandshake},
        daemon=True,
        name="BootInit",
    )
    init_thread.start()
    threading.Thread(
        target=_log_when_listening,
        args=(server, config.host, config.port, process_started),
        daemon=True,
        name="BootListenLog",
    ).start()
    exit_code = 0
    try:
        server.run()
    except Exception as e:  # uvicorn can raise bind/ssl/runtime errors; report the root cause and exit 1
        logger.error("Web server error: %s", safe_exc(e))
        logger.debug("Web server traceback", exc_info=True)
        exit_code = 1
    finally:
        # A still-running boot thread notices shutdown via ``server.should_exit`` (gateway handshake loop).
        init_thread.join(timeout=15)
        if role == "gateway":
            from .gateway_link import gateway_link_worker

            gateway_link_worker.stop()
        if role != "gateway":
            for label, module_name, attr in (
                ("AcquisitionWorker", "acquisition_worker", "acquisition_worker"),
                ("ArtistRefreshWorker", "artist_refresh_worker", "artist_refresh_worker"),
                ("ScrobbleWorker", "scrobble_worker", "scrobble_worker"),
                ("MixWorker", "mix_worker", "mix_worker"),
                ("ImportListWorker", "import_list_worker", "import_list_worker"),
                ("LibraryHealthWorker", "library_health", "library_health_worker"),
                ("SeedCleanupWorker", "seed_cleanup", "seed_cleanup_worker"),
                ("RecycleBinWorker", "recycle_bin", "recycle_bin_worker"),
            ):
                try:
                    module = __import__(f"trackseerr.{module_name}", fromlist=[attr])
                    getattr(module, attr).stop()
                except (ImportError, AttributeError, RuntimeError, OSError) as e:
                    logger.warning("Failed to stop %s cleanly: %s", label, safe_exc(e))
            from . import art_pipeline

            art_pipeline.stop_startup_backfill()
            art_pipeline.art_backfill_scheduler.stop()
            art_pipeline.shutdown()  # cancel queued pre-cache/thumbnail work so exit never drains it
            try:
                from .backlog_worker import backlog_worker, rss_worker

                backlog_worker.stop()
                rss_worker.stop()
                from .pending_worker import pending_worker

                pending_worker.stop()
            except (ImportError, AttributeError, RuntimeError, OSError) as e:
                logger.warning("Failed to stop backlog/RSS workers cleanly: %s", safe_exc(e))
        db.close()

    if exit_code or failure["code"]:
        return exit_code or failure["code"]
    logger.info("TrackSeerr server terminated cleanly.")
    return 0


def run(argv: Optional[list[str]] = None) -> int:
    """Entry point for ``python -m trackseerr``: ``init-dmz`` is a subcommand; no args runs the server."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "init-dmz":
        from .init_dmz import main as init_dmz_main

        return init_dmz_main(args[1:])
    return main()


if __name__ == "__main__":
    sys.exit(run())
