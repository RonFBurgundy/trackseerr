"""Response models for ``api/routes/delay_profiles.py`` (only the routes the router did not already type)."""

from trackseerr.api.response_models import ApiModel


class DeletedResponse(ApiModel):
    status: str
    id: int
