"""Response models for ``api/routes/notifications.py`` (only the routes the router did not already type)."""

from trackseerr.api.response_models import ApiModel


class DeletedResponse(ApiModel):
    status: str
    id: str
