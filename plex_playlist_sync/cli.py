import logging
from logging.handlers import RotatingFileHandler
import os
import signal
import sqlite3
import sys
import threading
import time
from typing import Optional, Union

import uvicorn

from .api.app import create_app
from .api.routes.sync import sync_state
from .api.routes.system import get_log_file_path, log_ring_buffer
from .clients.deezer import DeezerClient
from .clients.plex import PlexClient
from .clients.spotify import SpotifyClient
from .clients.spotify_scraper import SpotifyWebScraper
from .config import Config
from .redaction import redact_sensitive_query, redact_text, safe_exc
from .security import safe_data_path
from .storage import Database
from .sync import SyncCoordinator

logger = logging.getLogger("plex_playlist_sync")
_shutdown_requested = False


class RedactAccessLogFilter(logging.Filter):
    """Strips secrets (webhook token, API keys, OAuth state) from uvicorn access-log request paths."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(redact_sensitive_query(a) if isinstance(a, str) else a for a in record.args)
        if isinstance(record.msg, str):
            record.msg = redact_sensitive_query(record.msg)
        return True


class RedactLogFilter(logging.Filter):
    """Handler-level filter: renders the record and redacts invite/reset tokens and secret query params."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            return True
        redacted = redact_text(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        # Exception text and stack dumps are rendered later by Formatter.format; pre-render and redact
        # them here (Formatter reuses a cached ``exc_text``) so tracebacks cannot leak tokens either.
        if record.exc_info and not record.exc_text:
            try:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            except (TypeError, ValueError, AttributeError):
                record.exc_text = None
        if record.exc_text:
            record.exc_text = redact_text(record.exc_text)
        if record.stack_info:
            record.stack_info = redact_text(record.stack_info)
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


def setup_logging(level_name: str, config: Optional[Config] = None) -> None:
    numeric_level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        stream=sys.stdout,
        level=numeric_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)

    # Attach LogRingBuffer to root logger
    if log_ring_buffer not in root_logger.handlers:
        log_ring_buffer.setLevel(numeric_level)
        root_logger.addHandler(log_ring_buffer)

    # Attach RotatingFileHandler
    try:
        log_path = get_log_file_path(config)
        has_rfh = any(
            isinstance(h, RotatingFileHandler) and getattr(h, "baseFilename", "") == str(log_path)
            for h in root_logger.handlers
        )
        if not has_rfh:
            rfh = RotatingFileHandler(
                str(log_path),
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            rfh.setLevel(numeric_level)
            rfh.setFormatter(
                logging.Formatter(
                    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            root_logger.addHandler(rfh)
    except Exception as ex:
        logger.warning("Could not initialize RotatingFileHandler: %s", ex)

    # httpx/httpcore log every request URL at INFO, which would carry invite/reset tokens
    # relayed by the gateway. Keep them quiet, and redact whatever else reaches a handler.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    install_log_redaction(root_logger)


def main() -> int:
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    config = Config.from_env()
    setup_logging(config.log_level, config=config)

    logger.info("Initializing TrackSeerr v1.0.0")

    role = os.getenv("ROLE", "all-in-one").lower().strip()

    if role in ("gateway", "core"):
        from plex_playlist_sync.internal_auth import MIN_SECRET_LENGTH, validate_secret_strength

        if not validate_secret_strength(config.internal_core_secret):
            logger.error(
                "ROLE=%s requires INTERNAL_CORE_SECRET of at least %d characters (generate one with: openssl rand -hex 32).",
                role,
                MIN_SECRET_LENGTH,
            )
            return 1

    if role != "gateway" and (not config.plex_url or not config.plex_token):
        logger.error("Missing mandatory environment variables: PLEX_URL and PLEX_TOKEN must be specified.")
        return 1

    plex_client: Optional[PlexClient] = None
    spotify_client: Optional[Union[SpotifyClient, SpotifyWebScraper]] = None
    deezer_client: Optional[DeezerClient] = None

    if role != "gateway":
        try:
            plex_client = PlexClient(
                base_url=config.plex_url,
                token=config.plex_token,
                verify_ssl=config.plex_verify_ssl,
            )
        except Exception as e:
            if config.run_once or config.headless:
                logger.error("Failed to connect to Plex Media Server: %s", safe_exc(e))
                logger.debug("Plex connect traceback", exc_info=True)
                return 1
            logger.warning(
                "Could not connect to Plex Server at %s on startup: %s. "
                "Starting Web Server; connection will be retried during sync.",
                redact_text(config.plex_url),
                safe_exc(e),
            )

        if config.has_spotify:
            try:
                spotify_client = SpotifyClient(
                    client_id=config.spotify_client_id,  # type: ignore
                    client_secret=config.spotify_client_secret,  # type: ignore
                )
            except Exception as e:
                logger.error("Failed to initialize Spotify client: %s. Falling back to web scraper.", e)
                spotify_client = SpotifyWebScraper()
        else:
            logger.info("No Spotify API credentials configured; activating keyless SpotifyWebScraper")
            spotify_client = SpotifyWebScraper()

        if config.has_deezer:
            try:
                deezer_client = DeezerClient()
            except Exception as e:
                logger.error("Failed to initialize Deezer client: %s. Skipping Deezer sync.", e)

        coordinator = SyncCoordinator(
            config=config,
            plex_client=plex_client,
            spotify_client=spotify_client,
            deezer_client=deezer_client,
        )

        # 1. Run-once / CLI mode
        if config.run_once:
            logger.info("RUN_ONCE enabled; running single sync cycle and exiting.")
            coordinator.run_sync_cycle()
            logger.info("TrackSeerr run-once completed cleanly.")
            return 0

        # 2. Headless mode (no web UI)
        if config.headless:
            logger.info("Running in HEADLESS loop mode.")
            while not _shutdown_requested:
                try:
                    coordinator.run_sync_cycle()
                except Exception as e:
                    logger.error("Unexpected error occurred during sync cycle: %s", safe_exc(e))
                    logger.debug("Sync cycle traceback", exc_info=True)
                slept = 0
                while slept < config.wait_seconds and not _shutdown_requested:
                    time.sleep(min(1, config.wait_seconds - slept))
                    slept += 1
            logger.info("TrackSeerr terminated cleanly.")
            return 0

    # 3. Web UI & REST Server Mode (Default)
    logger.info("Starting TrackSeerr Web Server on %s:%d (role=%s)", config.host, config.port, role)
    db_base_dir = config.config_dir if (os.getenv("CONFIG_DIR") or os.path.isdir("/config")) else config.data_dir
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

    # Auto-discover Plex Home users and populate database
    if role != "gateway" and plex_client is not None:
        try:
            home_users = plex_client.get_home_users()
            for u in home_users:
                uname = u.get("username") or u.get("name") or "Unknown"
                admin_flag = bool(u.get("is_admin", u.get("admin", False)))
                if db.is_tombstoned(str(u["id"])):
                    continue  # deleted by an admin; only an explicit restore lets them back in
                known = db.get_user(str(u["id"]))
                admin_flag = admin_flag or bool(known and known["is_admin"])
                db.upsert_user(
                    user_id=str(u["id"]),
                    username=str(uname),
                    email=u.get("email"),
                    is_admin=admin_flag,
                )
            logger.info("Successfully discovered %d Plex Home users", len(home_users))
        except Exception as e:
            logger.warning("Could not auto-discover Plex Home users on startup: %s", safe_exc(e))

    # Sync legacy config playlist IDs to DB if any
    if role != "gateway":
        for sp_id in config.spotify_playlist_ids:
            if not db.get_playlist(sp_id):
                db.upsert_playlist(sp_id, f"Spotify Playlist {sp_id}", service="spotify")
        for dz_id in config.deezer_playlist_ids:
            if not db.get_playlist(dz_id):
                db.upsert_playlist(dz_id, f"Deezer Playlist {dz_id}", service="deezer")

    # Start periodic background sync worker thread if wait_seconds > 0
    if role != "gateway" and config.wait_seconds > 0:
        def background_sync_worker():
            logger.info("Background sync scheduler started (interval: %d seconds)", config.wait_seconds)
            while not _shutdown_requested:
                slept = 0
                while slept < config.wait_seconds and not _shutdown_requested:
                    time.sleep(min(1, config.wait_seconds - slept))
                    slept += 1
                if _shutdown_requested:
                    break
                try:
                    logger.info("Triggering scheduled background synchronization...")
                    sync_state.execute_sync(
                        db=db,
                        config=config,
                        plex_client=plex_client,
                        spotify_client=spotify_client,
                        deezer_client=deezer_client,
                    )
                except Exception as e:
                    logger.error("Error in scheduled background sync: %s", safe_exc(e))
                    logger.debug("Scheduled sync traceback", exc_info=True)

        bg_thread = threading.Thread(
            target=background_sync_worker, daemon=True, name="ScheduledSyncWorker"
        )
        bg_thread.start()

    # Start periodic Lidarr auto-trickle worker thread if configured
    if role != "gateway":
        from plex_playlist_sync.clients.lidarr import LidarrClient
        from plex_playlist_sync.lidarr_queue import lidarr_worker

        def background_lidarr_trickle_worker():
            logger.info("Lidarr auto-trickle background runner started")
            last_run_time = 0.0
            while not _shutdown_requested:
                time.sleep(5)
                if _shutdown_requested:
                    break
                try:
                    lidarr_settings = db.get_lidarr_settings()
                    if not isinstance(lidarr_settings, dict):
                        continue
                    auto_trickle = bool(lidarr_settings.get("auto_trickle", config.lidarr_auto_trickle))
                    url = lidarr_settings.get("url") or config.lidarr_url
                    api_key = lidarr_settings.get("api_key") or config.lidarr_api_key

                    if not (auto_trickle and url and api_key):
                        continue

                    interval_min = int(
                        lidarr_settings.get("auto_trickle_interval_minutes")
                        or config.lidarr_auto_trickle_interval_minutes
                        or 30
                    )
                    interval_sec = max(60, interval_min * 60)
                    now = time.time()
                    if now - last_run_time < interval_sec:
                        continue

                    last_run_time = now

                    if not lidarr_worker.is_running():
                        all_missing = db.get_missing_tracks()
                        unmonitored = [t for t in all_missing if t.get("lidarr_status") != "monitored"]
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
                            auto_search = bool(
                                lidarr_settings.get("auto_search", config.lidarr_auto_search)
                            )
                            root_folder = lidarr_settings.get("root_folder") or config.lidarr_root_folder
                            qp_id = lidarr_settings.get("quality_profile_id") or config.lidarr_quality_profile_id
                            mp_id = lidarr_settings.get("metadata_profile_id") or config.lidarr_metadata_profile_id

                            lidarr_cli = LidarrClient(
                                base_url=str(url),
                                api_key=str(api_key),
                                verify_ssl=config.plex_verify_ssl,
                                auto_search=auto_search,
                                root_folder=root_folder,
                                quality_profile_id=qp_id,
                                metadata_profile_id=mp_id,
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
                            )
                except Exception as e:
                    logger.error("Error in scheduled Lidarr auto-trickle: %s", safe_exc(e))
                    logger.debug("Auto-trickle traceback", exc_info=True)

        lidarr_bg_thread = threading.Thread(
            target=background_lidarr_trickle_worker, daemon=True, name="ScheduledLidarrTrickleWorker"
        )
        lidarr_bg_thread.start()

    # Start AcquisitionWorker for download monitoring and auto-organization
    if role != "gateway":
        from .acquisition_worker import acquisition_worker

        logger.info("Starting AcquisitionWorker (role=%s)", role)
        acquisition_worker.start(db=db, plex_client=plex_client, poll_interval=5.0)

        from .backlog_worker import backlog_worker, rss_worker

        if config.enable_backlog_search:
            logger.info(
                "Starting WantedBacklogWorker (interval: %d min)",
                config.backlog_search_interval_minutes,
            )
            backlog_worker.start(
                db=db,
                interval_seconds=config.backlog_search_interval_minutes * 60,
            )

        if config.enable_rss_sync:
            logger.info(
                "Starting RSSSyncWorker (interval: %d min)",
                config.rss_sync_interval_minutes,
            )
            rss_worker.start(
                db=db,
                interval_seconds=config.rss_sync_interval_minutes * 60,
            )

        from .artist_refresh_worker import artist_refresh_worker

        logger.info("Starting ArtistRefreshWorker (interval: 24h, pace: 1.5s)")
        artist_refresh_worker.start(db=db, interval_seconds=86400, pace_delay=1.5)

        from .scrobble_worker import scrobble_worker

        logger.info("Starting ScrobbleWorker (history poll + forward retry)")
        scrobble_worker.start(db=db, config=config)

        from .mix_worker import mix_worker

        logger.info("Starting MixWorker (hourly tailored mix regeneration)")
        mix_worker.start(db=db, config=config)

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
    try:
        server.run()
    except Exception as e:
        logger.error("Web server error: %s", safe_exc(e))
        logger.debug("Web server traceback", exc_info=True)
        return 1
    finally:
        if role != "gateway":
            try:
                from .acquisition_worker import acquisition_worker

                acquisition_worker.stop()
            except Exception:
                pass
            try:
                from .backlog_worker import backlog_worker, rss_worker

                backlog_worker.stop()
                rss_worker.stop()
            except Exception:
                pass
            try:
                from .artist_refresh_worker import artist_refresh_worker

                artist_refresh_worker.stop()
            except Exception:
                pass
            try:
                from .scrobble_worker import scrobble_worker

                scrobble_worker.stop()
            except Exception as e:
                logger.warning("Failed to stop ScrobbleWorker cleanly: %s", safe_exc(e))
            try:
                from .mix_worker import mix_worker

                mix_worker.stop()
            except Exception as e:
                logger.warning("Failed to stop MixWorker cleanly: %s", safe_exc(e))
        db.close()

    logger.info("TrackSeerr server terminated cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
