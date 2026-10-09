"""Response models for the routes of ``api/routes/import_lists.py`` that had no model.

The list, create, get, update, test and items routes are already typed through return-annotated models in the router
(``ImportList`` and friends); their ``config`` masks secrets (``********``), so those models are left as they are.
"""

from typing import Optional

from trackseerr.api.response_models import ApiModel


class ProviderField(ApiModel):
    key: str
    label: str
    type: str
    required: bool
    options: Optional[list[str]] = None


class ProviderMeta(ApiModel):
    provider: str
    label: str
    sources: list[str]
    fields: list[ProviderField]


class ImportListDeleted(ApiModel):
    status: str
    id: str


class ImportListQueued(ApiModel):
    queued: bool
