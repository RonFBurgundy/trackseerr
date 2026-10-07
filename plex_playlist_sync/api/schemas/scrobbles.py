"""Response models for ``/api/scrobbles`` (``api/routes/scrobbles.py``).

Skipped: ``/lastfm/callback`` (303 redirect). Secrets (Last.fm session key, ListenBrainz token, API secret) are never
declared: configs expose ``*_connected`` booleans and the server config a masked key only. The webhook URL carries the
Plex webhook secret and is admin only.
"""

from typing import Optional

from plex_playlist_sync.api.response_models import ApiModel


class WebhookStatus(ApiModel):
    status: str


class WebhookUrl(ApiModel):
    url: str


class AuthUrl(ApiModel):
    url: str


class ScrobbleConfig(ApiModel):
    user_id: str
    username: Optional[str] = None
    scrobbling_enabled: bool
    lastfm_connected: bool
    lastfm_username: Optional[str] = None
    listenbrainz_connected: bool
    listenbrainz_username: Optional[str] = None
    updated_at: Optional[str] = None


class Listen(ApiModel):
    id: int
    artist: str
    title: str
    album: Optional[str] = None
    played_at: str
    source: str
    lastfm_status: str
    listenbrainz_status: str


class ServerConfig(ApiModel):
    lastfm_configured: bool
    lastfm_api_key_masked: str
    lastfm_from_env: bool
    plex_history_poll_minutes: int
