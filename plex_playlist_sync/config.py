import os
import re
from dataclasses import dataclass, field
from typing import List, Optional


def _parse_bool(val: Optional[str], default: bool = False) -> bool:
    if val is None:
        return default
    clean = val.strip().lower()
    return clean in ("1", "true", "yes", "y", "on")


def _split_ids(val: Optional[str]) -> List[str]:
    if not val:
        return []
    # Split on commas, spaces, or semicolons
    raw_tokens = re.split(r"[\s,;]+", val.strip())
    # Extract clean ID if full Spotify or Deezer URL/URI was provided
    cleaned = []
    for token in raw_tokens:
        token = token.strip()
        if not token:
            continue
        # Spotify URI: spotify:playlist:37i9dQZF1DXcBWIGoYBM5M
        if token.startswith("spotify:playlist:"):
            token = token.split(":")[-1]
        # Spotify URL: https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=...
        elif "open.spotify.com/playlist/" in token:
            token = token.split("open.spotify.com/playlist/")[1].split("?")[0].strip("/")
        # Deezer URL: https://www.deezer.com/us/playlist/1313621735
        elif "deezer.com/" in token and "/playlist/" in token:
            token = token.split("/playlist/")[1].split("?")[0].strip("/")
        cleaned.append(token)
    return cleaned


@dataclass
class Config:
    plex_url: str
    plex_token: str
    plex_verify_ssl: bool = True

    write_missing_as_csv: bool = False
    append_service_suffix: bool = True
    add_playlist_poster: bool = True
    add_playlist_description: bool = True
    append_instead_of_sync: bool = False
    wait_seconds: int = 86400
    run_once: bool = False

    search_similarity_threshold: float = 0.9
    log_level: str = "INFO"
    data_dir: str = "/data"
    config_dir: str = "/config"

    spotify_client_id: Optional[str] = None
    spotify_client_secret: Optional[str] = None
    spotify_user_id: Optional[str] = None
    spotify_playlist_ids: List[str] = field(default_factory=list)

    deezer_user_id: Optional[str] = None
    deezer_playlist_ids: List[str] = field(default_factory=list)

    port: int = 5250
    host: str = "0.0.0.0"
    headless: bool = False

    lidarr_url: Optional[str] = None
    lidarr_api_key: Optional[str] = None
    lidarr_auto_search: bool = True
    lidarr_root_folder: Optional[str] = None
    lidarr_trickle_rate_seconds: float = 3.0
    lidarr_trickle_batch_size: int = 25
    lidarr_auto_trickle: bool = False
    lidarr_auto_trickle_interval_minutes: int = 30
    feed_token: Optional[str] = None
    auto_approve_requests: bool = False
    user_request_quota: int = 25  # deprecated: per-type quotas live in general_settings; only seeds them once (migration v28)
    enable_backlog_search: bool = True
    backlog_search_interval_minutes: int = 60
    enable_rss_sync: bool = True
    rss_sync_interval_minutes: int = 15
    enable_import_lists: bool = True
    application_url: Optional[str] = None
    role: str = "all-in-one"
    trackseerr_core_url: Optional[str] = None
    internal_core_secret: Optional[str] = None
    trusted_proxies: Optional[str] = None
    lastfm_api_key: Optional[str] = None
    lastfm_api_secret: Optional[str] = None

    @classmethod
    def from_env(cls) -> "Config":
        plex_url = os.getenv("PLEX_URL", "").strip()
        plex_token = os.getenv("PLEX_TOKEN", "").strip()

        # SSL verification toggle (check PLEX_VERIFY_SSL or IGNORE_SSL / IGNORE_SSC)
        verify_ssl = True
        if os.getenv("PLEX_VERIFY_SSL") is not None:
            verify_ssl = _parse_bool(os.getenv("PLEX_VERIFY_SSL"), True)
        elif _parse_bool(os.getenv("IGNORE_SSL"), False) or _parse_bool(os.getenv("IGNORE_SSC"), False):
            verify_ssl = False

        # One-shot / Cron mode
        run_once = _parse_bool(os.getenv("RUN_ONCE"), False) or _parse_bool(os.getenv("CRON"), False)

        # Spotify playlist IDs
        sp_ids_raw = os.getenv("SPOTIFY_PLAYLIST_ID") or os.getenv("SPOTIFY_PLAYLIST_IDS")
        sp_ids = _split_ids(sp_ids_raw)

        # Deezer playlist IDs
        dz_ids_raw = os.getenv("DEEZER_PLAYLIST_ID") or os.getenv("DEEZER_PLAYLIST_IDS")
        dz_ids = _split_ids(dz_ids_raw)

        # Similarity threshold
        try:
            threshold = float(os.getenv("SEARCH_SIMILARITY_THRESHOLD", "0.9"))
        except ValueError:
            threshold = 0.9

        # Wait seconds
        try:
            wait_sec = int(os.getenv("SECONDS_TO_WAIT", "86400"))
        except ValueError:
            wait_sec = 86400

        data_path = os.getenv("DATA_DIR", "/data").strip()
        config_path = (os.getenv("CONFIG_DIR") or ("/config" if os.path.isdir("/config") else os.getenv("DATA_DIR", "/data"))).strip()

        try:
            port = int(os.getenv("PORT", "5250"))
        except ValueError:
            port = 5250

        host = os.getenv("HOST", "0.0.0.0").strip() or "0.0.0.0"
        headless = _parse_bool(os.getenv("HEADLESS"), False)

        return cls(
            plex_url=plex_url,
            plex_token=plex_token,
            plex_verify_ssl=verify_ssl,
            write_missing_as_csv=_parse_bool(os.getenv("WRITE_MISSING_AS_CSV"), False),
            append_service_suffix=_parse_bool(os.getenv("APPEND_SERVICE_SUFFIX"), True),
            add_playlist_poster=_parse_bool(os.getenv("ADD_PLAYLIST_POSTER"), True),
            add_playlist_description=_parse_bool(os.getenv("ADD_PLAYLIST_DESCRIPTION"), True),
            append_instead_of_sync=_parse_bool(os.getenv("APPEND_INSTEAD_OF_SYNC"), False),
            wait_seconds=wait_sec,
            run_once=run_once,
            search_similarity_threshold=threshold,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            data_dir=data_path,
            config_dir=config_path,
            spotify_client_id=os.getenv("SPOTIFY_CLIENT_ID") or None,
            spotify_client_secret=os.getenv("SPOTIFY_CLIENT_SECRET") or None,
            spotify_user_id=os.getenv("SPOTIFY_USER_ID") or None,
            spotify_playlist_ids=sp_ids,
            deezer_user_id=os.getenv("DEEZER_USER_ID") or None,
            deezer_playlist_ids=dz_ids,
            port=port,
            host=host,
            headless=headless,
            lidarr_url=os.getenv("LIDARR_URL") or None,
            lidarr_api_key=os.getenv("LIDARR_API_KEY") or None,
            lidarr_auto_search=_parse_bool(os.getenv("LIDARR_AUTO_SEARCH"), True),
            lidarr_root_folder=os.getenv("LIDARR_ROOT_FOLDER") or None,
            lidarr_trickle_rate_seconds=float(os.getenv("LIDARR_TRICKLE_RATE_SECONDS")) if os.getenv("LIDARR_TRICKLE_RATE_SECONDS") else 3.0,
            lidarr_trickle_batch_size=int(os.getenv("LIDARR_TRICKLE_BATCH_SIZE")) if os.getenv("LIDARR_TRICKLE_BATCH_SIZE") and os.getenv("LIDARR_TRICKLE_BATCH_SIZE").isdigit() else 25,
            lidarr_auto_trickle=_parse_bool(os.getenv("LIDARR_AUTO_TRICKLE"), False),
            lidarr_auto_trickle_interval_minutes=int(os.getenv("LIDARR_AUTO_TRICKLE_INTERVAL_MINUTES")) if os.getenv("LIDARR_AUTO_TRICKLE_INTERVAL_MINUTES") and os.getenv("LIDARR_AUTO_TRICKLE_INTERVAL_MINUTES").isdigit() else 30,
            feed_token=os.getenv("FEED_TOKEN") or None,
            auto_approve_requests=_parse_bool(os.getenv("AUTO_APPROVE_REQUESTS"), False),
            user_request_quota=int(os.getenv("USER_REQUEST_QUOTA")) if os.getenv("USER_REQUEST_QUOTA") and os.getenv("USER_REQUEST_QUOTA").isdigit() else 25,
            enable_backlog_search=_parse_bool(os.getenv("ENABLE_BACKLOG_SEARCH"), True),
            backlog_search_interval_minutes=int(os.getenv("BACKLOG_SEARCH_INTERVAL_MINUTES")) if os.getenv("BACKLOG_SEARCH_INTERVAL_MINUTES") and os.getenv("BACKLOG_SEARCH_INTERVAL_MINUTES").isdigit() else 60,
            enable_rss_sync=_parse_bool(os.getenv("ENABLE_RSS_SYNC"), True),
            rss_sync_interval_minutes=int(os.getenv("RSS_SYNC_INTERVAL_MINUTES")) if os.getenv("RSS_SYNC_INTERVAL_MINUTES") and os.getenv("RSS_SYNC_INTERVAL_MINUTES").isdigit() else 15,
            enable_import_lists=_parse_bool(os.getenv("ENABLE_IMPORT_LISTS"), True),
            application_url=(os.getenv("APPLICATION_URL") or os.getenv("APP_URL") or "").strip().rstrip("/") or None,
            role=os.getenv("ROLE", "all-in-one").lower().strip() or "all-in-one",
            trackseerr_core_url=os.getenv("TRACKSEERR_CORE_URL", "").rstrip("/") or None,
            internal_core_secret=os.getenv("INTERNAL_CORE_SECRET", "").strip() or None,
            trusted_proxies=os.getenv("TRUSTED_PROXIES", "").strip() or None,
            lastfm_api_key=os.getenv("LASTFM_API_KEY", "").strip() or None,
            lastfm_api_secret=os.getenv("LASTFM_API_SECRET", "").strip() or None,
        )

    @property
    def has_spotify(self) -> bool:
        has_auth = bool(self.spotify_client_id and self.spotify_client_secret)
        has_targets = bool(self.spotify_user_id or self.spotify_playlist_ids)
        return has_auth and has_targets

    @property
    def has_deezer(self) -> bool:
        return bool(self.deezer_user_id or self.deezer_playlist_ids)

    @property
    def has_lidarr(self) -> bool:
        return bool(self.lidarr_url and self.lidarr_api_key)
