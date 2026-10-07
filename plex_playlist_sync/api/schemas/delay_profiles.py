"""Response models for ``api/routes/delay_profiles.py`` (only the routes the router did not already type)."""

from plex_playlist_sync.api.response_models import ApiModel


class DeletedResponse(ApiModel):
    status: str
    id: int
