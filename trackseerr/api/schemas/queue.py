"""Response models for ``/api/queue`` (``api/routes/queue.py``). The list item model stays in the router."""

from trackseerr.api.response_models import ApiModel


class QueueCancelResponse(ApiModel):
    status: str
    id: str
